"""Planner service for Stage 4 bounded replan loop."""

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
from foundation.models.file import (
    FileApplyDiffRequest,
    FileEditRequest,
    FileReadChunkRequest,
    FileReadRequest,
    FileWriteRequest,
)
from foundation.models.git import (
    GitCommitRequest,
    GitDiffRequest,
    GitLogRequest,
    GitShowRequest,
    GitStageRequest,
    GitStatusRequest,
    GitUnstageRequest,
)
from foundation.services.capabilities import GIT_CAPABILITY_ID, CapabilityRegistry
from foundation.services.provider import ProviderAdapter, ProviderError, ProviderErrorCode
from foundation.services.tools import GitContextRequest, LocalToolService, ToolExecutionError
from foundation.settings import ApprovalMode

_MAX_PLAN_ACTIONS = 10
_MAX_TOTAL_ACTIONS = 50


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
        observation_messages: list[ProviderMessage] | None = None,
        iteration: int = 1,
        remaining_actions: int = _MAX_TOTAL_ACTIONS,
    ) -> tuple[AssistantPlan, ProviderResponseMetadata]:
        # Fold observation into the system prompt instead of appending as
        # multi-turn messages.  Many structured-output models (Qwen 3.x)
        # generate empty responses when the conversation alternates
        # assistant→system after the user turn.
        observation_text: str | None = None
        if observation_messages:
            observation_text = "\n\n".join(m.content for m in observation_messages)
        base_messages = self._base_plan_messages(
            request, context,
            iteration=iteration,
            remaining_actions=remaining_actions,
            observation_text=observation_text,
        )
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
        *,
        iteration: int = 1,
        remaining_actions: int = _MAX_TOTAL_ACTIONS,
        observation_text: str | None = None,
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
        effective_max = min(_MAX_PLAN_ACTIONS, remaining_actions)
        instructions = (
            "You are the planning model for Foundation CLI v3 Stage 4. "
            "You MUST respond with a single JSON object matching the action "
            "shape guide below. Do NOT include markdown fences, commentary, "
            "or any text outside the JSON object. "
            f"This is iteration {iteration} of a bounded replan loop. "
            f"Return at most {effective_max} actions. "
            "Prefer typed file capabilities (foundation.file.read, "
            "foundation.file.write, foundation.file.edit, foundation.file.apply_diff) "
            "for reading and editing files. "
            "Prefer typed git capabilities (foundation.git.*) for repository "
            "inspection and staging. "
            "The git.commit capability requires approval and never stages implicitly. "
            "Do NOT use shell commands for operations that have typed "
            "capability equivalents. "
            "Use shell actions for running tests, builds, linters, and "
            "environment inspection only. "
            "Shell args are passed directly to the target binary via execve, "
            "NOT interpreted by a shell. Do NOT wrap args in single or double "
            "quotes, do NOT expect glob expansion or variable substitution, "
            "and pass each argument as a separate string. For example, for "
            "`gh api users/x --jq '.name'` use "
            "`command=\"gh\", args=[\"api\", \"users/x\", \"--jq\", \".name\"]` — "
            "no surrounding quotes on the jq expression. "
            "Do not assume command or tool output before execution. "
            "If verification (tests, type checks, linters) fails, diagnose the error and issue "
            "repair actions in the next iteration. "
            "For code-changing turns, run at least one relevant verification command before "
            "completing with zero actions, unless verification is unavailable and you explain why. "
            "If an action is risky, mutating, networked, or uncertain, mark requires_approval=true "
            "and explain why in approval_reason. "
            "If the user can be answered directly, return zero actions with your final answer. "
            "When returning zero actions to finish, your assistant_message "
            "becomes the user-facing answer. "
            "Available capability snapshot:\n"
            f"{json.dumps(capability_guide, indent=2)}\n"
            "Action shape guide:\n"
            f"{json.dumps(schema_outline, indent=2)}\n"
            "Context JSON:\n"
            f"{json.dumps(
                context.model_dump(mode='json', exclude={'available_capabilities'}),
                indent=2,
            )}"
        )
        if observation_text:
            instructions += f"\n\n{observation_text}"
        user_content = (
            f"{request.message}\n\n"
            "Respond with a JSON object only. No markdown, no commentary."
        )
        return [
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=instructions),
            *request.conversation_history,
            ProviderMessage(role=ProviderMessageRole.USER, content=user_content),
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
        endpoint = manifest.runtime_endpoint
        if endpoint == "builtin.search":
            from foundation.services.tools import SearchRequest

            SearchRequest.model_validate(arguments)
            return
        if endpoint == "builtin.files":
            from foundation.services.tools import FileDiscoveryRequest

            FileDiscoveryRequest.model_validate(arguments)
            return
        if endpoint == "builtin.git":
            GitContextRequest.model_validate(arguments)
            return
        if endpoint == "builtin.man":
            from foundation.services.tools import HelpLookupRequest, HelpLookupSource

            HelpLookupRequest.model_validate(
                {**arguments, "source": HelpLookupSource.MAN}
            )
            return
        if endpoint == "builtin.tldr":
            from foundation.services.tools import HelpLookupRequest, HelpLookupSource

            HelpLookupRequest.model_validate(
                {**arguments, "source": HelpLookupSource.TLDR}
            )
            return
        if endpoint == "builtin.shell":
            from foundation.models import ShellAction

            ShellAction.model_validate(arguments)
            return
        _FILE_VALIDATORS: dict[str, type] = {
            "builtin.file.read": FileReadRequest,
            "builtin.file.read_chunk": FileReadChunkRequest,
            "builtin.file.write": FileWriteRequest,
            "builtin.file.edit": FileEditRequest,
            "builtin.file.apply_diff": FileApplyDiffRequest,
        }
        if endpoint in _FILE_VALIDATORS:
            _FILE_VALIDATORS[endpoint].model_validate(arguments)
            return
        _GIT_VALIDATORS: dict[str, type] = {
            "builtin.git.status": GitStatusRequest,
            "builtin.git.diff": GitDiffRequest,
            "builtin.git.show": GitShowRequest,
            "builtin.git.log": GitLogRequest,
            "builtin.git.stage": GitStageRequest,
            "builtin.git.unstage": GitUnstageRequest,
            "builtin.git.commit": GitCommitRequest,
        }
        if endpoint in _GIT_VALIDATORS:
            _GIT_VALIDATORS[endpoint].model_validate(arguments)
            return
        raise PlanningError(f"Unsupported capability id: {capability_id}")
