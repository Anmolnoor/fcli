"""Planner service for Stage 4 bounded replan loop."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

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
    FileWriteBriefRequest,
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

_MAX_PLAN_ACTIONS = 40
_MAX_TOTAL_ACTIONS = 200
_COMMIT_INTENT_WORD_RE = re.compile(r"\bcommit(?:ing|ted|s)?\b", re.IGNORECASE)
_COMMIT_INTENT_PHRASES = (
    "stop for approval",
    "approve the change",
)
_SHELL_EQUIVALENT_COMMANDS = {
    "cat": "foundation.file.read or foundation.file.read_chunk",
    "grep": "foundation.search",
    "printf": "foundation.file.write or foundation.file.edit",
}


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
            str(item.capability_id) for item in capability_snapshots if item.kind.value == "tool"
        ]
        notes: list[str] = []
        git_context: dict[str, object] | None = None

        if any(str(item.capability_id) == GIT_CAPABILITY_ID for item in capability_snapshots):
            try:
                git_result = self._tool_service.git_context(
                    GitContextRequest(
                        scope=Path(request_cwd),
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
            request,
            context,
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
                repairable = exc.code in (
                    ProviderErrorCode.INVALID_RESPONSE,
                    ProviderErrorCode.TRUNCATED,
                )
                if repairable and attempt < self._max_plan_attempts:
                    if exc.code is ProviderErrorCode.TRUNCATED:
                        feedback = (
                            "Your previous response was truncated before the JSON closed. "
                            "Produce a SHORTER plan: do not inline large file contents — for "
                            "any sizable file body, omit `content` and provide a brief "
                            "`content_brief` describing what to write instead."
                        )
                    else:
                        feedback = "The previous response was not valid JSON."
                    supplemental_messages = self._repair_messages(
                        feedback,
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
                self._validate_supported_actions(
                    plan,
                    request=request,
                    context=context,
                )
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
                        "_file_write_note": (
                            "for foundation.file.write use {path, content} for tiny "
                            "files, or {path, content_brief} (NOT content) for "
                            "anything longer — the body is generated separately"
                        ),
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
            "For foundation.file.write, do NOT inline a large file body in the "
            "`content` argument — long content bloats this JSON plan and can be "
            "truncated. For anything beyond a few short lines, omit `content` and "
            "instead provide `content_brief`: a concise description of what the file "
            "should contain. The body is generated separately. Use literal `content` "
            "only for very short files. "
            "Prefer typed git capabilities (foundation.git.*) for repository "
            "inspection and staging. "
            "The git.commit capability requires approval and never stages implicitly. "
            "If the user asked to commit or stop for approval and git context "
            "shows staged changes, do not finish with zero actions. Plan "
            "foundation.git.commit with requires_approval=true instead. "
            "Do NOT use shell commands for operations that have typed "
            "capability equivalents. "
            "In particular, do NOT use shell cat, grep, or printf for file "
            "reading, search, or file edits. "
            "Use shell actions for running tests, builds, linters, and "
            "environment inspection only. "
            "Shell args are passed directly to the target binary via execve, "
            "NOT interpreted by a shell. Do NOT wrap args in single or double "
            "quotes, do NOT expect glob expansion or variable substitution, "
            "and pass each argument as a separate string. For example, for "
            "`gh api users/x --jq '.name'` use "
            '`command="gh", args=["api", "users/x", "--jq", ".name"]` — '
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
            f"{
                json.dumps(
                    context.model_dump(mode='json', exclude={'available_capabilities'}),
                    indent=2,
                )
            }"
        )
        if observation_text:
            instructions += f"\n\n{observation_text}"
        user_content = (
            f"{request.message}\n\nRespond with a JSON object only. No markdown, no commentary."
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

    def _validate_supported_actions(
        self,
        plan: AssistantPlan,
        *,
        request: UserRequest,
        context: ContextSnapshot,
    ) -> None:
        if len(plan.actions) > _MAX_PLAN_ACTIONS:
            raise PlanningError(f"Structured plans are bounded to {_MAX_PLAN_ACTIONS} actions.")
        if (
            not plan.actions
            and self._has_commit_intent(request.message)
            and self._context_has_staged_changes(context)
        ):
            raise PlanningError(
                "Zero-action completion is invalid when the user asked for commit "
                "approval and git context still shows staged changes. Plan "
                "foundation.git.commit with requires_approval=true."
            )
        for action in plan.actions:
            if action.kind is ActionKind.SHELL:
                assert action.shell is not None
                if any(character.isspace() for character in action.shell.command):
                    raise PlanningError(
                        f"Shell action {action.id!r} must split the executable and args."
                    )
                shell_command = action.shell.command.split("/")[-1]
                if shell_command in _SHELL_EQUIVALENT_COMMANDS:
                    equivalent = _SHELL_EQUIVALENT_COMMANDS[shell_command]
                    raise PlanningError(
                        f"Shell action {action.id!r} uses `{shell_command}`, but "
                        f"the typed capability equivalent {equivalent} must be used."
                    )
            if action.kind is ActionKind.TOOL_CALL:
                assert action.tool_call is not None
                if (
                    action.tool_call.capability_id == "foundation.git.commit"
                    and not action.requires_approval
                ):
                    raise PlanningError(
                        "foundation.git.commit must set requires_approval=true "
                        "and provide approval_reason."
                    )
                self._validated_tool_request(
                    action.tool_call.capability_id,
                    action.tool_call.version,
                    action.tool_call.arguments,
                )

    @staticmethod
    def _has_commit_intent(message: str) -> bool:
        if _COMMIT_INTENT_WORD_RE.search(message):
            return True
        lower = message.lower()
        return any(phrase in lower for phrase in _COMMIT_INTENT_PHRASES)

    @staticmethod
    def _context_has_staged_changes(context: ContextSnapshot) -> bool:
        if not isinstance(context.git_context, dict):
            return False
        staged_diff = context.git_context.get("staged_diff")
        if isinstance(staged_diff, list) and bool(staged_diff):
            return True
        status_entries = context.git_context.get("status")
        if not isinstance(status_entries, list):
            return False
        for entry in status_entries:
            if not isinstance(entry, dict):
                continue
            index_status = entry.get("index_status")
            if not isinstance(index_status, str):
                continue
            stripped = index_status.strip()
            # Treat untracked (`?`) and ignored (`!`) entries as not-staged.
            # A clean index reports a single space; only real index ops
            # (M / A / D / R / C / U) constitute "staged changes".
            if stripped and stripped not in {"?", "!"}:
                return True
        return False

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

            HelpLookupRequest.model_validate({**arguments, "source": HelpLookupSource.MAN})
            return
        if endpoint == "builtin.tldr":
            from foundation.services.tools import HelpLookupRequest, HelpLookupSource

            HelpLookupRequest.model_validate({**arguments, "source": HelpLookupSource.TLDR})
            return
        if endpoint == "builtin.shell":
            from foundation.models import ShellAction

            ShellAction.model_validate(arguments)
            return
        if endpoint == "builtin.file.write":
            has_content = bool(arguments.get("content"))
            has_brief = bool(arguments.get("content_brief"))
            if has_content and has_brief:
                raise PlanningError(
                    "foundation.file.write must provide either content or "
                    "content_brief, not both."
                )
            if has_brief:
                FileWriteBriefRequest.model_validate(arguments)
            else:
                FileWriteRequest.model_validate(arguments)
            return
        _FILE_VALIDATORS: dict[str, type[BaseModel]] = {
            "builtin.file.read": FileReadRequest,
            "builtin.file.read_chunk": FileReadChunkRequest,
            "builtin.file.edit": FileEditRequest,
            "builtin.file.apply_diff": FileApplyDiffRequest,
        }
        if endpoint in _FILE_VALIDATORS:
            _FILE_VALIDATORS[endpoint].model_validate(arguments)
            return
        _GIT_VALIDATORS: dict[str, type[BaseModel]] = {
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
