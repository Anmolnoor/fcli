"""Hardening stage 7: isolated unit tests for PlannerService.

These tests exercise the planner directly (observation injection, plan-time
endpoint validation, plan repair, and the `_validate_supported_actions`
guards) without going through the orchestrator loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundation.models import (
    AssistantPlan,
    CapabilityId,
    CapabilityInstallSource,
    CapabilityKind,
    CapabilityManifest,
    CapabilityTransport,
    CapabilityVersion,
    ContextSnapshot,
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
    RiskClass,
    TrustTier,
    UserRequest,
)
from foundation.services import LocalToolService
from foundation.services.capabilities import CapabilityRegistry, CapabilityStore
from foundation.services.planner import PlannerService, PlanningError
from foundation.services.provider import ProviderError, ProviderErrorCode
from foundation.settings import ApprovalMode


def _provider_response(payload: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(payload),
        structured_output=payload,
        metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
    )


def _text_response(body: str) -> ProviderResponse:
    return ProviderResponse(
        content=body,
        structured_output=None,
        metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
    )


class StubProvider:
    """Queue-backed provider stub mirroring the orchestrator test pattern."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[ProviderPrompt] = []

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        if prompt.schema_name == "assistant_plan_review" and not (
            self._responses
            and isinstance(self._responses[0].structured_output, dict)
            and "decision" in self._responses[0].structured_output
        ):
            return _provider_response(
                {
                    "decision": "accept",
                    "reason": "Stub preflight accepted the candidate plan.",
                }
            )
        self.calls.append(prompt)
        if not self._responses:
            return _provider_response({"assistant_message": "Done.", "actions": []})
        return self._responses.pop(0)


class ErrorThenPlanProvider:
    """Raise one ProviderError on the first call, then return the queued plan."""

    def __init__(self, error: ProviderError, response: ProviderResponse) -> None:
        self._error: ProviderError | None = error
        self._response = response
        self.calls: list[ProviderPrompt] = []

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        self.calls.append(prompt)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return self._response


def _build_planner(
    tmp_path: Path,
    provider: Any,
    *,
    max_plan_attempts: int = 2,
) -> tuple[PlannerService, CapabilityRegistry, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    tool_service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    registry = CapabilityRegistry(
        store=CapabilityStore(tmp_path / "capabilities"),
        tool_service=tool_service,
    )
    planner = PlannerService(
        workspace_root=str(workspace_root),
        approval_mode=ApprovalMode.PROMPT,
        provider=provider,
        tool_service=tool_service,
        capability_registry=registry,
        max_plan_attempts=max_plan_attempts,
    )
    return planner, registry, workspace_root


def _context(
    workspace_root: Path,
    *,
    git_context: dict[str, Any] | None = None,
) -> ContextSnapshot:
    return ContextSnapshot(
        workspace_root=str(workspace_root),
        request_cwd=str(workspace_root),
        approval_mode="prompt",
        git_context=git_context,
    )


def _plan(actions: list[dict[str, Any]], message: str = "Working on it.") -> AssistantPlan:
    return AssistantPlan.model_validate({"assistant_message": message, "actions": actions})


def _shell_action(
    action_id: str,
    command: str,
    args: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "shell",
        "summary": f"Run {command}",
        "shell": {"command": command, "args": args or []},
    }


def _tool_action(
    action_id: str,
    capability_id: str,
    arguments: dict[str, Any],
    *,
    requires_approval: bool = False,
    approval_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "tool_call",
        "summary": f"Call {capability_id}",
        "requires_approval": requires_approval,
        "approval_reason": approval_reason,
        "tool_call": {"capability_id": capability_id, "arguments": arguments},
    }


def _register_bogus_capability(registry: CapabilityRegistry) -> None:
    registry.register(
        CapabilityManifest(
            capability_id=CapabilityId(root="foundation.bogus"),
            version=CapabilityVersion(root="1.0.0"),
            kind=CapabilityKind.TOOL,
            name="Bogus Tool",
            description="Test-only capability whose runtime endpoint is unsupported.",
            transport=CapabilityTransport.BUILTIN_TOOL,
            runtime_endpoint="builtin.bogus",
            input_schema={"type": "object"},
            install_source=CapabilityInstallSource(kind="test", location="test://bogus"),
            owner="tests",
            risk_class=RiskClass.LOW,
            trust_tier=TrustTier.FOUNDATION,
        )
    )


# ---------------------------------------------------------------------------
# Observation injection
# ---------------------------------------------------------------------------


def test_observation_and_iteration_details_reach_planning_prompt(tmp_path: Path) -> None:
    provider = StubProvider(
        [_provider_response({"assistant_message": "Investigation complete.", "actions": []})]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, metadata = planner.request_plan(
        UserRequest(message="fix the failing test"),
        _context(workspace_root),
        request_id="req-1",
        observation_messages=[
            ProviderMessage(
                role=ProviderMessageRole.ASSISTANT,
                content="Observation block: pytest exited 1 in iteration 2.",
            ),
            ProviderMessage(
                role=ProviderMessageRole.DEVELOPER,
                content="Remaining budget is shrinking.",
            ),
        ],
        iteration=3,
        remaining_actions=12,
    )

    assert plan.assistant_message == "Investigation complete."
    assert metadata.provider == "stub"
    assert len(provider.calls) == 1
    prompt = provider.calls[0]
    assert prompt.schema_name == "assistant_plan"
    developer = prompt.messages[0]
    assert developer.role is ProviderMessageRole.DEVELOPER
    # Observation messages are folded into the developer instructions, joined
    # by blank lines, rather than appended as extra conversation turns.
    assert "Observation block: pytest exited 1 in iteration 2." in developer.content
    assert "Remaining budget is shrinking." in developer.content
    assert "This is iteration 3" in developer.content
    assert "Return at most 12 actions" in developer.content
    user = prompt.messages[-1]
    assert user.role is ProviderMessageRole.USER
    assert "fix the failing test" in user.content


# ---------------------------------------------------------------------------
# Plan-time endpoint validation
# ---------------------------------------------------------------------------


def test_valid_typed_file_and_git_plan_passes_validation(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Inspecting the workspace.",
                    "actions": [
                        _tool_action("read_app", "foundation.file.read", {"path": "src/app.py"}),
                        _tool_action("repo_status", "foundation.git.status", {}),
                    ],
                }
            )
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="what changed?"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert len(provider.calls) == 1
    assert [action.tool_call.capability_id for action in plan.actions if action.tool_call] == [
        "foundation.file.read",
        "foundation.git.status",
    ]
    # Defaults: iteration 1, full action budget capped at the plan bound.
    developer = provider.calls[0].messages[0]
    assert "This is iteration 1" in developer.content
    assert "Return at most 40 actions" in developer.content


def test_unknown_runtime_endpoint_is_rejected(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Calling a capability with no executor.",
                    "actions": [_tool_action("bogus_call", "foundation.bogus", {})],
                }
            )
        ]
    )
    planner, registry, workspace_root = _build_planner(tmp_path, provider, max_plan_attempts=1)
    _register_bogus_capability(registry)

    with pytest.raises(PlanningError) as excinfo:
        planner.request_plan(
            UserRequest(message="do something"),
            _context(workspace_root),
            request_id="req-1",
        )

    assert "Unsupported capability id: foundation.bogus" in str(excinfo.value)
    assert "after 1 attempt(s)" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Plan repair
# ---------------------------------------------------------------------------


def test_missing_structured_output_triggers_repair_retry(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _text_response("plain prose, not the requested JSON object"),
            _provider_response({"assistant_message": "Recovered.", "actions": []}),
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="say hello"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert plan.assistant_message == "Recovered."
    assert len(provider.calls) == 2
    retry = provider.calls[1]
    assert len(retry.messages) == len(provider.calls[0].messages) + 1
    repair = retry.messages[-1]
    assert repair.role is ProviderMessageRole.DEVELOPER
    assert "omitted the required JSON object" in repair.content
    assert "Return a corrected JSON object only" in repair.content
    # First attempt decodes deterministically; the retry nudges temperature.
    assert provider.calls[0].temperature is None
    assert retry.temperature == 0.4


def test_invalid_action_shape_triggers_repair_with_validation_feedback(tmp_path: Path) -> None:
    bad_payload = {
        "assistant_message": "Doing work.",
        "actions": [
            {
                "id": "broken",
                "kind": "shell",
                "summary": "Shell kind with tool payload",
                "tool_call": {
                    "capability_id": "foundation.file.read",
                    "arguments": {"path": "x"},
                },
            }
        ],
    }
    provider = StubProvider(
        [
            _provider_response(bad_payload),
            _provider_response({"assistant_message": "Fixed.", "actions": []}),
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="read a file"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert plan.assistant_message == "Fixed."
    assert len(provider.calls) == 2
    retry = provider.calls[1]
    # The invalid output is echoed back as an assistant turn before the
    # developer repair instruction.
    assert retry.messages[-2].role is ProviderMessageRole.ASSISTANT
    assert retry.messages[-2].content == json.dumps(bad_payload)
    assert retry.messages[-1].role is ProviderMessageRole.DEVELOPER
    assert "The previous JSON failed validation" in retry.messages[-1].content


def test_truncated_response_repair_requests_content_brief(tmp_path: Path) -> None:
    partial = '{"assistant_message":"writing","actions":[{"id":"w"'
    provider = ErrorThenPlanProvider(
        ProviderError(
            "Provider response was truncated before completion.",
            code=ProviderErrorCode.TRUNCATED,
            response_text=partial,
        ),
        _provider_response({"assistant_message": "Shorter plan.", "actions": []}),
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="write a big file"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert plan.assistant_message == "Shorter plan."
    assert len(provider.calls) == 2
    retry = provider.calls[1]
    assert retry.messages[-2].role is ProviderMessageRole.ASSISTANT
    assert retry.messages[-2].content == partial
    assert "truncated before the JSON closed" in retry.messages[-1].content
    assert "content_brief" in retry.messages[-1].content
    assert retry.temperature == 0.4


def test_non_repairable_provider_error_propagates_unwrapped(tmp_path: Path) -> None:
    provider = ErrorThenPlanProvider(
        ProviderError("connection refused", code=ProviderErrorCode.NETWORK),
        _provider_response({"assistant_message": "Never reached.", "actions": []}),
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    with pytest.raises(ProviderError) as excinfo:
        planner.request_plan(
            UserRequest(message="say hello"),
            _context(workspace_root),
            request_id="req-1",
        )

    assert excinfo.value.code is ProviderErrorCode.NETWORK
    assert len(provider.calls) == 1


def test_planning_error_after_exhausted_attempts(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _text_response("still not JSON"),
            _text_response("again not JSON"),
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    with pytest.raises(PlanningError) as excinfo:
        planner.request_plan(
            UserRequest(message="say hello"),
            _context(workspace_root),
            request_id="req-1",
        )

    assert "after 2 attempt(s)" in str(excinfo.value)
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Zero-action commit-intent guard
# ---------------------------------------------------------------------------


def test_zero_action_plan_with_commit_intent_and_staged_changes_is_rejected(
    tmp_path: Path,
) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    context = _context(
        workspace_root,
        git_context={"status": [{"index_status": "M", "path": "src/app.py"}]},
    )

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            _plan([], message="All done."),
            request=UserRequest(message="Please commit the staged changes."),
            context=context,
        )

    assert "Zero-action completion is invalid" in str(excinfo.value)
    assert "foundation.git.commit" in str(excinfo.value)


def test_zero_action_plan_allowed_when_nothing_is_staged(tmp_path: Path) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    # Untracked-only status entries do not count as staged changes.
    context = _context(
        workspace_root,
        git_context={"status": [{"index_status": "?", "path": "scratch.txt"}], "staged_diff": []},
    )

    planner._validate_supported_actions(
        _plan([], message="Nothing to commit."),
        request=UserRequest(message="Please commit the staged changes."),
        context=context,
    )


# ---------------------------------------------------------------------------
# Shell action guards
# ---------------------------------------------------------------------------


def test_gh_api_raw_output_flag_is_rejected(tmp_path: Path) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    plan = _plan(
        [_shell_action("fetch_readme", "gh", ["api", "repos/x/y/readme", "--jq", ".content", "-r"])]
    )

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            plan,
            request=UserRequest(message="fetch my GitHub README"),
            context=_context(workspace_root),
        )

    assert "gh api" in str(excinfo.value)
    assert "does not support `-r`" in str(excinfo.value)


@pytest.mark.parametrize(
    ("command", "expected_equivalent"),
    [
        ("cat", "foundation.file.read"),
        ("grep", "foundation.search"),
        ("printf", "foundation.file.write"),
        ("/bin/cat", "foundation.file.read"),
    ],
)
def test_shell_commands_with_typed_equivalents_are_rejected(
    tmp_path: Path,
    command: str,
    expected_equivalent: str,
) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    plan = _plan([_shell_action("use_shell", command, ["some-target"])])

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            plan,
            request=UserRequest(message="inspect the file"),
            context=_context(workspace_root),
        )

    assert expected_equivalent in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tool-call guards
# ---------------------------------------------------------------------------


def test_git_commit_action_must_require_approval(tmp_path: Path) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    plan = _plan(
        [_tool_action("commit_work", "foundation.git.commit", {"message": "feat: add thing"})]
    )

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            plan,
            request=UserRequest(message="commit the staged work"),
            context=_context(workspace_root),
        )

    assert "requires_approval=true" in str(excinfo.value)


def test_deferred_write_following_earlier_actions_is_rejected(tmp_path: Path) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    plan = _plan(
        [
            _shell_action("list_dir", "ls", []),
            _tool_action(
                "write_report",
                "foundation.file.write",
                {"path": "report.md", "content_brief": "a report based on the listing"},
            ),
        ]
    )

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            plan,
            request=UserRequest(message="summarize the directory"),
            context=_context(workspace_root),
        )

    assert "content_brief cannot follow earlier" in str(excinfo.value)


def test_file_write_with_both_content_and_brief_is_rejected(tmp_path: Path) -> None:
    planner, _registry, workspace_root = _build_planner(tmp_path, StubProvider([]))
    plan = _plan(
        [
            _tool_action(
                "write_notes",
                "foundation.file.write",
                {"path": "notes.md", "content": "hello", "content_brief": "greeting"},
            )
        ]
    )

    with pytest.raises(PlanningError) as excinfo:
        planner._validate_supported_actions(
            plan,
            request=UserRequest(message="write the notes file"),
            context=_context(workspace_root),
        )

    assert "either content or content_brief, not both" in str(excinfo.value)


def test_request_plan_truncates_plan_before_deferred_write(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Listing first, then writing.",
                    "actions": [
                        _shell_action("list_dir", "ls", []),
                        _tool_action(
                            "write_report",
                            "foundation.file.write",
                            {"path": "report.md", "content_brief": "a directory report"},
                        ),
                    ],
                }
            )
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="summarize the directory"),
        _context(workspace_root),
        request_id="req-1",
    )

    # Rather than rejecting outright, request_plan keeps the executable prefix
    # and drops the deferred write so it can be replanned with observations.
    assert len(provider.calls) == 1
    assert plan.assistant_message == "Listing first, then writing."
    assert [action.id for action in plan.actions] == ["list_dir"]


# ---------------------------------------------------------------------------
# Preflight review and payload normalization
# ---------------------------------------------------------------------------


def test_preflight_reject_returns_zero_action_answer(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing the notes file.",
                    "actions": [
                        _tool_action(
                            "write_notes",
                            "foundation.file.write",
                            {"path": "notes.md", "content": "hello"},
                        )
                    ],
                }
            ),
            _provider_response(
                {
                    "decision": "reject",
                    "reason": "Execution should not proceed.",
                }
            ),
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="write the notes file"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert plan.actions == []
    assert plan.assistant_message == "Execution should not proceed."
    assert len(provider.calls) == 2
    review_prompt = provider.calls[1]
    assert review_prompt.schema_name == "assistant_plan_review"
    assert "write the notes file" in review_prompt.messages[-1].content


def test_file_write_note_is_normalized_into_content_brief(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing notes.",
                    "actions": [
                        {
                            "id": "write_notes",
                            "kind": "tool_call",
                            "summary": "Write the notes file",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "_file_write_note": "content_brief: a short summary of the run",
                                "arguments": {"path": "notes.md"},
                            },
                        }
                    ],
                }
            )
        ]
    )
    planner, _registry, workspace_root = _build_planner(tmp_path, provider)

    plan, _metadata = planner.request_plan(
        UserRequest(message="write the notes file"),
        _context(workspace_root),
        request_id="req-1",
    )

    assert len(plan.actions) == 1
    tool_call = plan.actions[0].tool_call
    assert tool_call is not None
    assert tool_call.arguments == {
        "path": "notes.md",
        "content_brief": "a short summary of the run",
    }
