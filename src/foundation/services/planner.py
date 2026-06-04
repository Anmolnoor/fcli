"""Planner service for Stage 4 bounded replan loop."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

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
# Temperature used on a plan repair retry so the model doesn't deterministically
# reproduce the same invalid/empty response it produced at temperature 0.
_PLAN_REPAIR_TEMPERATURE = 0.4
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
_FILE_WRITE_NOTE_KEY = "_file_write_note"
_CONTENT_BRIEF_PREFIX = "content_brief:"
_GH_API_UNSUPPORTED_FLAGS = frozenset({"-r"})
_FILE_MUTATION_CAPABILITY_IDS = frozenset(
    {
        "foundation.file.write",
        "foundation.file.edit",
        "foundation.file.apply_diff",
    }
)
_RELATIVE_PATH_MUTATION_COMMANDS = frozenset(
    {
        "cp",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "sed",
        "tee",
        "touch",
    }
)
_DIRECTORY_TARGET_RE = re.compile(r"\b(dir|directory|folder|path)\b", re.IGNORECASE)
_GIT_MUTATION_CAPABILITY_IDS = frozenset(
    {
        "foundation.git.stage",
        "foundation.git.unstage",
        "foundation.git.commit",
    }
)
_SHELL_MUTATION_COMMANDS = frozenset({*_RELATIVE_PATH_MUTATION_COMMANDS, "git"})
_GIT_MUTATION_SUBCOMMANDS = frozenset(
    {
        "add",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "mv",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
        "tag",
    }
)


class PlanningError(RuntimeError):
    """Raised when the planner cannot produce a valid bounded plan."""


class PlanReview(BaseModel):
    """Structured preflight review for a candidate plan before execution."""

    decision: Literal["accept", "repair", "reject"]
    reason: str = Field(min_length=1)
    repaired_plan: AssistantPlan | None = None

    @model_validator(mode="after")
    def _validate_repair_payload(self) -> PlanReview:
        if self.decision == "repair" and self.repaired_plan is None:
            raise ValueError("repair decisions must include repaired_plan")
        return self


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
            # First attempt decodes deterministically (temperature 0). On a
            # repair retry, nudge temperature up so the model doesn't
            # deterministically reproduce the same malformed/empty response.
            retry_temperature = _PLAN_REPAIR_TEMPERATURE if attempt > 1 else None
            prompt = ProviderPrompt(
                messages=[*base_messages, *supplemental_messages],
                response_format=ProviderResponseFormat.JSON_OBJECT,
                schema_name="assistant_plan",
                output_schema=AssistantPlan.model_json_schema(),
                temperature=retry_temperature,
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
                structured_output = self._normalize_plan_payload(response.structured_output)
                plan = AssistantPlan.model_validate(structured_output)
                prefix_plan = self._plan_with_executable_prefix(plan)
                if prefix_plan is not None:
                    candidate = prefix_plan
                    self._validate_supported_actions(
                        candidate,
                        request=request,
                        context=context,
                    )
                    candidate = self._preflight_review_plan(
                        candidate,
                        request=request,
                        context=context,
                        observation_text=observation_text,
                    )
                    self._validate_supported_actions(
                        candidate,
                        request=request,
                        context=context,
                    )
                    return candidate, response.metadata
                self._validate_supported_actions(
                    plan,
                    request=request,
                    context=context,
                )
                plan = self._preflight_review_plan(
                    plan,
                    request=request,
                    context=context,
                    observation_text=observation_text,
                )
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
                    "kind": "explanation | shell | tool_call | question",
                    "summary": "short description",
                    "requires_approval": "boolean",
                    "approval_reason": "string | null",
                    "explanation": "required for explanation actions",
                    "question": {
                        "prompt": "required for question actions",
                        "options": ["optional", "choices"],
                        "allow_free_text": "boolean",
                    },
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
                        "arguments": (
                            "tool-specific JSON object; for foundation.file.write use "
                            "{path, content} for tiny files, or {path, content_brief} "
                            "(NOT content) for anything longer — the body is generated "
                            "separately. Never omit both content and content_brief."
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
            "only for very short files. Because `content_brief` bodies are generated "
            "before the iteration's actions execute, a file.write using `content_brief` "
            "must be the first action in the plan and must rely only on data already "
            "available in the prompt/observations. If the body depends on shell/tool "
            "output, gather that data first and write the file in a later iteration. "
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
            "To read a file outside the workspace root, issue a normal "
            "foundation.file.read with its absolute path; the user will be asked "
            "to approve out-of-scope read access rather than it being silently "
            "blocked. Do not refuse preemptively. "
            "Writes, edits, apply-diffs, and shell commands that create or mutate "
            "files outside the workspace root are hard-blocked by policy and cannot "
            "be approved from inside this workspace. If the user asks for that, "
            "return zero actions and explain that they should open fcli in the "
            "target directory or use that directory as the workspace root. "
            "Do not reinterpret a user-named ancestor directory as a relative "
            "workspace child. For example, if the workspace is `~/Developer/fcli` "
            "and the user asks for the `Developer` directory, do not use "
            "`developer/...`; that points inside the current workspace. "
            "Shell args are passed directly to the target binary via execve, "
            "NOT interpreted by a shell. Do NOT wrap args in single or double "
            "quotes, do NOT expect glob expansion or variable substitution, "
            "and pass each argument as a separate string. For example, for "
            "`gh api users/x --jq '.name'` use "
            '`command="gh", args=["api", "users/x", "--jq", ".name"]` — '
            "no surrounding quotes on the jq expression. Shell actions are independent: "
            "stdout from one shell action is not piped into later shell actions. If a "
            "pipeline is truly needed, use one explicit shell action such as "
            '`command="bash", args=["-c", "cmd1 | cmd2"]`, or gather output and replan. '
            "Do not assume command or tool output before execution. "
            "If verification (tests, type checks, linters) fails, diagnose the error and issue "
            "repair actions in the next iteration. "
            "For code-changing turns, run at least one relevant verification command before "
            "completing with zero actions, unless verification is unavailable and you explain why. "
            "If an action is risky, mutating, networked, or uncertain, mark requires_approval=true "
            "and explain why in approval_reason. "
            "If the request is genuinely ambiguous or you are missing information only the "
            "user can provide, emit a single `question` action (kind=question) with a clear "
            "prompt and optional options, rather than guessing. Use this sparingly. "
            "If the user can be answered directly, return zero actions with your final answer. "
            "For direct-answer zero-action turns, keep the answer concise and task-focused. "
            "Do not mirror casual address terms from the user (for example, buddy, bro, boss, "
            "sir, mate) unless the user explicitly asks you to use that style. "
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

    @staticmethod
    def _normalize_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
        actions = payload.get("actions")
        if not isinstance(actions, list):
            return payload

        normalized_actions: list[Any] = []
        changed = False
        for action in actions:
            if not isinstance(action, dict):
                normalized_actions.append(action)
                continue

            tool_call = action.get("tool_call")
            if not isinstance(tool_call, dict) or _FILE_WRITE_NOTE_KEY not in tool_call:
                normalized_actions.append(action)
                continue
            if tool_call.get("capability_id") != "foundation.file.write":
                normalized_actions.append(action)
                continue
            arguments = tool_call.get("arguments")
            note = tool_call.get(_FILE_WRITE_NOTE_KEY)
            if not isinstance(arguments, dict) or not isinstance(note, str):
                normalized_actions.append(action)
                continue

            brief = PlannerService._content_brief_from_file_write_note(note)
            has_body = bool(arguments.get("content") or arguments.get("content_brief"))
            if not brief and not has_body:
                normalized_actions.append(action)
                continue

            normalized_tool_call = dict(tool_call)
            normalized_tool_call.pop(_FILE_WRITE_NOTE_KEY)
            changed = True

            if brief and not has_body:
                normalized_tool_call["arguments"] = {**arguments, "content_brief": brief}

            normalized_actions.append({**action, "tool_call": normalized_tool_call})

        if not changed:
            return payload
        return {**payload, "actions": normalized_actions}

    @staticmethod
    def _content_brief_from_file_write_note(note: str) -> str:
        stripped = note.strip()
        if stripped.lower().startswith(_CONTENT_BRIEF_PREFIX):
            return stripped[len(_CONTENT_BRIEF_PREFIX) :].strip()
        return stripped

    @staticmethod
    def _plan_with_executable_prefix(plan: AssistantPlan) -> AssistantPlan | None:
        for action_index, action in enumerate(plan.actions):
            if action_index == 0:
                continue
            if action.kind is not ActionKind.TOOL_CALL or action.tool_call is None:
                continue
            if action.tool_call.capability_id != "foundation.file.write":
                continue
            arguments = action.tool_call.arguments
            has_content = "content" in arguments
            has_brief = "content_brief" in arguments
            body_is_generated_too_early = has_brief and not has_content
            body_is_missing = not has_content and not has_brief
            if body_is_generated_too_early or body_is_missing:
                return plan.model_copy(update={"actions": plan.actions[:action_index]})
        return None

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

    def _preflight_review_plan(
        self,
        plan: AssistantPlan,
        *,
        request: UserRequest,
        context: ContextSnapshot,
        observation_text: str | None,
    ) -> AssistantPlan:
        if not self._plan_needs_preflight_review(plan):
            return plan
        response = self._provider.complete(
            ProviderPrompt(
                messages=self._preflight_review_messages(
                    plan,
                    request=request,
                    context=context,
                    observation_text=observation_text,
                ),
                response_format=ProviderResponseFormat.JSON_OBJECT,
                schema_name="assistant_plan_review",
                output_schema=PlanReview.model_json_schema(),
            )
        )
        if response.structured_output is None:
            raise PlanningError("Preflight plan review did not return structured output.")
        review = PlanReview.model_validate(response.structured_output)
        if review.decision == "accept":
            return plan
        if review.decision == "repair":
            assert review.repaired_plan is not None
            return review.repaired_plan
        return AssistantPlan(assistant_message=review.reason, actions=[])

    @staticmethod
    def _preflight_review_messages(
        plan: AssistantPlan,
        *,
        request: UserRequest,
        context: ContextSnapshot,
        observation_text: str | None,
    ) -> list[ProviderMessage]:
        developer = (
            "You are the preflight plan reviewer for Foundation CLI. Review the "
            "candidate plan before any action executes. Return JSON only. "
            "Accept plans that faithfully satisfy the user request and stay within "
            "the workspace/policy constraints. Repair only when the corrected plan "
            "is obvious from the request and context. Reject by returning a "
            "zero-action repaired_plan or decision=reject when execution should not "
            "proceed. Check path intent, workspace root, request cwd, outside-write "
            "policy, missing prerequisites, repeated actions, typed-tool usage, and "
            "whether code-changing plans include a plausible verification path."
        )
        review_context = {
            "request": request.message,
            "context": context.model_dump(mode="json", exclude={"available_capabilities"}),
            "candidate_plan": plan.model_dump(mode="json"),
        }
        if observation_text:
            review_context["observations"] = observation_text
        user = (
            "Review this candidate plan and return one of:\n"
            "- accept: keep the candidate plan unchanged\n"
            "- repair: include repaired_plan with the corrected AssistantPlan\n"
            "- reject: no action should execute; reason becomes the user-facing answer\n\n"
            f"{json.dumps(review_context, indent=2)}"
        )
        return [
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=developer),
            ProviderMessage(role=ProviderMessageRole.USER, content=user),
        ]

    @staticmethod
    def _plan_needs_preflight_review(plan: AssistantPlan) -> bool:
        for action in plan.actions:
            if action.kind is ActionKind.TOOL_CALL and action.tool_call is not None:
                capability_id = action.tool_call.capability_id
                if (
                    capability_id in _FILE_MUTATION_CAPABILITY_IDS
                    or capability_id in _GIT_MUTATION_CAPABILITY_IDS
                ):
                    return True
            if action.kind is ActionKind.SHELL and action.shell is not None:
                command = action.shell.command.split("/")[-1]
                if command in _SHELL_MUTATION_COMMANDS:
                    if command != "git":
                        return True
                    if action.shell.args and action.shell.args[0] in _GIT_MUTATION_SUBCOMMANDS:
                        return True
        return False

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
        named_ancestors = self._named_workspace_ancestors(request.message, context)
        for action_index, action in enumerate(plan.actions):
            if action.kind is ActionKind.SHELL:
                assert action.shell is not None
                if any(character.isspace() for character in action.shell.command):
                    raise PlanningError(
                        f"Shell action {action.id!r} must split the executable and args."
                    )
                shell_command = action.shell.command.split("/")[-1]
                if (
                    shell_command == "gh"
                    and action.shell.args[:1] == ["api"]
                    and any(arg in _GH_API_UNSUPPORTED_FLAGS for arg in action.shell.args[1:])
                ):
                    raise PlanningError(
                        f"Shell action {action.id!r} uses `gh api`, which does not "
                        "support `-r`. Put raw-output behavior inside the jq "
                        "expression or decode the output with an explicit shell."
                    )
                if shell_command in _SHELL_EQUIVALENT_COMMANDS:
                    equivalent = _SHELL_EQUIVALENT_COMMANDS[shell_command]
                    raise PlanningError(
                        f"Shell action {action.id!r} uses `{shell_command}`, but "
                        f"the typed capability equivalent {equivalent} must be used."
                    )
                for raw_path in self._shell_mutation_path_args(shell_command, action.shell.args):
                    self._reject_ambiguous_ancestor_relative_path(
                        action_id=action.id,
                        raw_path=raw_path,
                        named_ancestors=named_ancestors,
                    )
            if action.kind is ActionKind.TOOL_CALL:
                assert action.tool_call is not None
                if (
                    action.tool_call.capability_id == "foundation.file.write"
                    and bool(action.tool_call.arguments.get("content_brief"))
                    and action_index > 0
                ):
                    raise PlanningError(
                        "foundation.file.write with content_brief cannot follow earlier "
                        "actions in the same plan because the body is generated before "
                        "actions run. First run data-gathering actions, then write in "
                        "the next iteration using observations."
                    )
                if (
                    action.tool_call.capability_id == "foundation.git.commit"
                    and not action.requires_approval
                ):
                    raise PlanningError(
                        "foundation.git.commit must set requires_approval=true "
                        "and provide approval_reason."
                    )
                if action.tool_call.capability_id in _FILE_MUTATION_CAPABILITY_IDS:
                    self._reject_ambiguous_ancestor_relative_path(
                        action_id=action.id,
                        raw_path=action.tool_call.arguments.get("path"),
                        named_ancestors=named_ancestors,
                    )
                self._validated_tool_request(
                    action.tool_call.capability_id,
                    action.tool_call.version,
                    action.tool_call.arguments,
                )

    @staticmethod
    def _named_workspace_ancestors(
        message: str,
        context: ContextSnapshot,
    ) -> dict[str, str]:
        if not _DIRECTORY_TARGET_RE.search(message):
            return {}
        message_lower = message.lower()
        workspace_root = Path(context.workspace_root).expanduser().resolve()
        ancestors: dict[str, str] = {}
        for ancestor in workspace_root.parents:
            name = ancestor.name
            if len(name) < 3:
                continue
            name_lower = name.lower()
            if re.search(rf"\b{re.escape(name_lower)}\b", message_lower):
                ancestors[name_lower] = name
        return ancestors

    @staticmethod
    def _shell_mutation_path_args(command: str, args: list[str]) -> list[str]:
        if command not in _RELATIVE_PATH_MUTATION_COMMANDS:
            return []
        return [arg for arg in args if arg and not arg.startswith("-")]

    @staticmethod
    def _reject_ambiguous_ancestor_relative_path(
        *,
        action_id: str,
        raw_path: object,
        named_ancestors: dict[str, str],
    ) -> None:
        if not named_ancestors or not isinstance(raw_path, str):
            return
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            return
        parts = [part for part in candidate.parts if part not in {"", "."}]
        if not parts:
            return
        ancestor_name = named_ancestors.get(parts[0].lower())
        if ancestor_name is None:
            return
        raise PlanningError(
            f"Action {action_id!r} uses relative path {raw_path!r} after the "
            f"user named the outside-workspace ancestor directory {ancestor_name!r}. "
            "Do not create a same-named directory inside the workspace; return "
            "zero actions explaining that outside-workspace writes are blocked, "
            "or ask a question if the target is ambiguous."
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
            has_content = "content" in arguments
            has_brief = "content_brief" in arguments
            if has_content and has_brief:
                raise PlanningError(
                    "foundation.file.write must provide either content or content_brief, not both."
                )
            if not has_content and not has_brief:
                raise PlanningError(
                    "foundation.file.write must provide either content or content_brief."
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
