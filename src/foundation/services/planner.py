"""Planner service for Stage 3 runtime splitting."""

from __future__ import annotations

import json

from pydantic import ValidationError

from foundation.models import (
    ActionKind,
    AssistantPlan,
    ContextSnapshot,
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
    ProviderResponseMetadata,
    UserRequest,
)
from foundation.services.capabilities import GIT_CAPABILITY_ID, CapabilityRegistry
from foundation.services.provider import ProviderAdapter, ProviderError, ProviderErrorCode
from foundation.services.tools import GitContextRequest, LocalToolService, ToolExecutionError
from foundation.settings import ApprovalMode

_MAX_PLAN_ACTIONS = 5


class PlanningError(RuntimeError):
    """Raised when the planner cannot produce a valid bounded plan."""


class PlannerService:
    """Gather context and request a bounded structured plan from the provider."""

    def __init__(
        self,
        *,
        workspace_root: str,
        approval_mode: ApprovalMode,
        provider: ProviderAdapter,
        tool_service: LocalToolService,
        capability_registry: CapabilityRegistry,
        max_plan_attempts: int = 2,
    ) -> None:
        self._workspace_root = workspace_root
        self._approval_mode = approval_mode
        self._provider = provider
        self._tool_service = tool_service
        self._capability_registry = capability_registry
        self._max_plan_attempts = max_plan_attempts

    def gather_context(self, *, request_cwd: str) -> ContextSnapshot:
        capability_snapshots = self._capability_registry.planner_snapshot()
        available_tools = [
            str(item.capability_id)
            for item in capability_snapshots
            if item.kind.value == "tool"
        ]
        notes: list[str] = []
        git_context: dict[str, object] | None = None

        if any(str(item.capability_id) == GIT_CAPABILITY_ID for item in capability_snapshots):
            try:
                git_result = self._tool_service.git_context(
                    GitContextRequest(
                        scope=request_cwd,
                        max_status_entries=10,
                        max_recent_commits=3,
                    )
                )
            except ToolExecutionError as exc:
                notes.append(f"Git context unavailable: {exc.error.message}")
            else:
                git_context = git_result.model_dump(mode="json")

        return ContextSnapshot(
            workspace_root=self._workspace_root,
            request_cwd=request_cwd,
            approval_mode=self._approval_mode.value,
            available_tools=available_tools,
            available_capabilities=capability_snapshots,
            git_context=git_context,
            notes=notes,
        )

    def request_plan(
        self,
        request: UserRequest,
        context: ContextSnapshot,
        *,
        request_id: str,
    ) -> tuple[AssistantPlan, ProviderResponseMetadata]:
        base_messages = self._base_plan_messages(request, context)
        supplemental_messages: list[ProviderMessage] = []
        last_error: Exception | None = None

        for attempt in range(1, self._max_plan_attempts + 1):
            prompt = ProviderPrompt(
                messages=[*base_messages, *supplemental_messages],
                response_format=ProviderResponseFormat.JSON_OBJECT,
                schema_name="assistant_plan",
                output_schema=AssistantPlan.model_json_schema(),
            )
            try:
                response = self._provider.complete(prompt)
            except ProviderError as exc:
                last_error = exc
                if (
                    exc.code is ProviderErrorCode.INVALID_RESPONSE
                    and attempt < self._max_plan_attempts
                ):
                    supplemental_messages = self._repair_messages(
                        "The previous response was not valid JSON.",
                        invalid_output=exc.response_text,
                    )
                    continue
                raise

            if response.structured_output is None:
                last_error = PlanningError(
                    "Provider did not return structured output for the plan request."
                )
                if attempt < self._max_plan_attempts:
                    supplemental_messages = self._repair_messages(
                        "The previous response omitted the required JSON object."
                    )
                    continue
                break

            try:
                plan = AssistantPlan.model_validate(response.structured_output)
                self._validate_supported_actions(plan)
            except (ValidationError, PlanningError) as exc:
                last_error = exc
                if attempt < self._max_plan_attempts:
                    supplemental_messages = self._repair_messages(
                        f"The previous JSON failed validation: {exc}",
                        invalid_output=response.content,
                    )
                    continue
                break

            return plan, response.metadata

        detail = str(last_error) if last_error is not None else "Unknown planning failure."
        raise PlanningError(
            "The provider did not produce a valid structured plan after "
            f"{self._max_plan_attempts} attempt(s): {detail}"
        )

    def _base_plan_messages(
        self,
        request: UserRequest,
        context: ContextSnapshot,
    ) -> list[ProviderMessage]:
        schema_outline = {
            "assistant_message": "string",
            "actions": [
                {
                    "id": "unique action identifier",
                    "kind": "explanation | shell | tool_call",
                    "summary": "short description",
                    "requires_approval": "boolean",
                    "approval_reason": "string | null",
                    "explanation": "required for explanation actions",
                    "shell": {
                        "command": "string",
                        "args": ["string"],
                        "cwd": "string | null",
                        "timeout_seconds": "integer | null",
                        "mode": "buffered | stream | pty",
                    },
                    "tool_call": {
                        "capability_id": "foundation.search",
                        "version": "1.0.0 | null",
                        "arguments": "tool-specific JSON object",
                    },
                }
            ],
        }
        capability_guide = [
            {
                "capability_id": str(snapshot.capability_id),
                "version": str(snapshot.version),
                "name": snapshot.name,
                "description": snapshot.description,
                "transport": snapshot.transport.value,
                "risk_class": snapshot.risk_class.value,
                "trust_tier": snapshot.trust_tier.value,
                "declared_side_effects": list(snapshot.declared_side_effects),
                "input_schema": snapshot.input_schema,
            }
            for snapshot in context.available_capabilities
        ]
        instructions = (
            "You are the planning model for Foundation CLI v2 Stage 3. "
            f"Return at most {_MAX_PLAN_ACTIONS} actions. "
            "Prefer typed tool_call actions that reference one available capability. "
            "Use shell actions only for simple read-only inspection commands. "
            "Do not assume command or tool output before execution. "
            "If an action is risky, mutating, networked, or uncertain, mark requires_approval=true "
            "and explain why in approval_reason. "
            "If the user can be answered directly, return zero actions or an explanation action. "
            "Available capability snapshot:\n"
            f"{json.dumps(capability_guide, indent=2)}\n"
            "Action shape guide:\n"
            f"{json.dumps(schema_outline, indent=2)}\n"
            "Context JSON:\n"
            f"{json.dumps(context.model_dump(mode='json'), indent=2)}"
        )
        return [
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=instructions),
            *request.conversation_history,
            ProviderMessage(role=ProviderMessageRole.USER, content=request.message),
        ]

    def _repair_messages(
        self,
        validation_feedback: str,
        *,
        invalid_output: str | None = None,
    ) -> list[ProviderMessage]:
        messages: list[ProviderMessage] = []
        if invalid_output:
            messages.append(
                ProviderMessage(
                    role=ProviderMessageRole.ASSISTANT,
                    content=invalid_output,
                )
            )
        messages.append(
            ProviderMessage(
                role=ProviderMessageRole.DEVELOPER,
                content=(
                    f"{validation_feedback} Return a corrected JSON object only. "
                    "Do not include markdown fences."
                ),
            )
        )
        return messages

    def _validate_supported_actions(self, plan: AssistantPlan) -> None:
        if len(plan.actions) > _MAX_PLAN_ACTIONS:
            raise PlanningError(f"Structured plans are bounded to {_MAX_PLAN_ACTIONS} actions.")
        for action in plan.actions:
            if action.kind is ActionKind.SHELL:
                assert action.shell is not None
                if any(character.isspace() for character in action.shell.command):
                    raise PlanningError(
                        f"Shell action {action.id!r} must split the executable and args."
                    )
            if action.kind is ActionKind.TOOL_CALL:
                assert action.tool_call is not None
                self._validated_tool_request(
                    action.tool_call.capability_id,
                    action.tool_call.version,
                    action.tool_call.arguments,
                )

    def _validated_tool_request(
        self,
        capability_id: str,
        version: str | None,
        arguments: dict[str, object],
    ) -> None:
        manifest = self._capability_registry.resolve(capability_id, version)
        if manifest.runtime_endpoint == "builtin.search":
            from foundation.services.tools import SearchRequest

            SearchRequest.model_validate(arguments)
            return
        if manifest.runtime_endpoint == "builtin.files":
            from foundation.services.tools import FileDiscoveryRequest

            FileDiscoveryRequest.model_validate(arguments)
            return
        if manifest.runtime_endpoint == "builtin.git":
            GitContextRequest.model_validate(arguments)
            return
        if manifest.runtime_endpoint == "builtin.man":
            from foundation.services.tools import HelpLookupRequest, HelpLookupSource

            HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.MAN,
                }
            )
            return
        if manifest.runtime_endpoint == "builtin.tldr":
            from foundation.services.tools import HelpLookupRequest, HelpLookupSource

            HelpLookupRequest.model_validate(
                {
                    **arguments,
                    "source": HelpLookupSource.TLDR,
                }
            )
            return
        if manifest.runtime_endpoint == "builtin.shell":
            from foundation.models import ShellAction

            ShellAction.model_validate(arguments)
            return
        raise PlanningError(f"Unsupported capability id: {capability_id}")
