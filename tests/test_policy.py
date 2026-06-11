from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from foundation.models import (
    ActionKind,
    CapabilityApprovalRequest,
    CapabilityConstraintSet,
    CapabilityId,
    CapabilityInstallSource,
    CapabilityInvocationBudget,
    CapabilityKind,
    CapabilityManifest,
    CapabilityPolicyOutcome,
    CapabilityScopeKind,
    CapabilityScopeRule,
    CapabilityScopeTarget,
    CapabilitySideEffectRule,
    CapabilityTransport,
    CapabilityVersion,
    PlannedAction,
    PolicyReasonCode,
    RiskClass,
    ShellAction,
    ToolCall,
    TrustTier,
)
from foundation.services import (
    ApprovalService,
    CapabilityPolicyEngine,
    CapabilityRegistry,
    CapabilityStore,
    LocalToolService,
    SearchRequest,
    SearchResult,
)
from foundation.settings import ApprovalMode


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripts: dict[str, str] | None = None,
) -> tuple[CapabilityRegistry, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    if scripts:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, script in scripts.items():
            _write_executable(bin_dir / name, script)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    return (
        CapabilityRegistry(
            store=CapabilityStore(tmp_path / "capabilities"),
            tool_service=service,
        ),
        workspace_root,
    )


def test_policy_engine_allows_read_only_shell_with_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(tmp_path, monkeypatch)
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="show_cwd",
        kind=ActionKind.SHELL,
        summary="Show the current directory",
        shell=ShellAction(command="pwd"),
    )

    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.ALLOW_WITH_CONSTRAINTS
    assert evaluation.verdict.reason_codes == []
    assert evaluation.verdict.constraints is not None
    assert evaluation.verdict.constraints.invocation_budget is not None
    assert evaluation.verdict.constraints.invocation_budget.output_limit_kb == 256


def test_policy_engine_blocks_out_of_scope_tool_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(
        tmp_path,
        monkeypatch,
        scripts={
            "rg": "print('')\n",
            "git": "print('')\n",
        },
    )
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    outside_scope = tmp_path / "outside"
    outside_scope.mkdir()
    action = PlannedAction(
        id="search_outside",
        kind=ActionKind.TOOL_CALL,
        summary="Search outside the workspace",
        tool_call=ToolCall(
            capability_id="foundation.search",
            arguments={"query": "needle", "scope": str(outside_scope)},
        ),
    )

    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.BLOCK
    assert evaluation.verdict.reason_codes == [PolicyReasonCode.PATH_OUT_OF_SCOPE]


def test_policy_engine_allows_relative_file_read_path_within_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(tmp_path, monkeypatch)
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    (workspace_root / "notes.txt").write_text("hello\n", encoding="utf-8")
    action = PlannedAction(
        id="read_notes",
        kind=ActionKind.TOOL_CALL,
        summary="Read a workspace file by relative path",
        tool_call=ToolCall(
            capability_id="foundation.file.read",
            arguments={"path": "notes.txt"},
        ),
    )

    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.ALLOW_WITH_CONSTRAINTS
    assert evaluation.verdict.reason_codes == []


def test_policy_engine_blocks_relative_file_read_path_that_escapes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(tmp_path, monkeypatch)
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="read_outside",
        kind=ActionKind.TOOL_CALL,
        summary="Try to read outside the workspace with a relative path",
        tool_call=ToolCall(
            capability_id="foundation.file.read",
            arguments={"path": "../outside.txt"},
        ),
    )

    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.BLOCK
    assert evaluation.verdict.reason_codes == [PolicyReasonCode.PATH_OUT_OF_SCOPE]


def test_policy_engine_requires_approval_for_untrusted_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(
        tmp_path,
        monkeypatch,
        scripts={
            "rg": "print('')\n",
            "git": "print('')\n",
        },
    )
    registry.register(
        CapabilityManifest(
            capability_id=CapabilityId(root="user.search"),
            version=CapabilityVersion(root="1.0.0"),
            kind=CapabilityKind.TOOL,
            name="User Search",
            description="Run the built-in search transport from a user manifest.",
            transport=CapabilityTransport.BUILTIN_TOOL,
            runtime_endpoint="builtin.search",
            transport_config={"binary": "rg", "required": True},
            input_schema=SearchRequest.model_json_schema(),
            output_schema=SearchResult.model_json_schema(),
            install_source=CapabilityInstallSource(kind="local", location="/tmp/user.search"),
            owner="user",
            provenance="manual",
            risk_class=RiskClass.LOW,
            trust_tier=TrustTier.USER,
            declared_side_effects=["filesystem_read"],
            constraints=CapabilityConstraintSet(
                path_rules=[
                    CapabilityScopeRule(
                        target=CapabilityScopeTarget.PATH,
                        kind=CapabilityScopeKind.WORKSPACE,
                    )
                ],
                network_rules=[
                    CapabilityScopeRule(
                        target=CapabilityScopeTarget.NETWORK,
                        kind=CapabilityScopeKind.NONE,
                    )
                ],
                side_effect_rules=[CapabilitySideEffectRule(side_effect="filesystem_read")],
                invocation_budget=CapabilityInvocationBudget(
                    timeout_seconds=30,
                    output_limit_kb=64,
                ),
            ),
        )
    )
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="user_search",
        kind=ActionKind.TOOL_CALL,
        summary="Search with an untrusted capability",
        tool_call=ToolCall(
            capability_id="user.search",
            arguments={"query": "needle"},
        ),
    )

    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.REQUIRE_APPROVAL
    assert PolicyReasonCode.UNTRUSTED_CAPABILITY in evaluation.verdict.reason_codes


def test_policy_engine_enforces_invocation_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(
        tmp_path,
        monkeypatch,
        scripts={
            "rg": "print('')\n",
            "git": "print('')\n",
        },
    )
    registry.register(
        CapabilityManifest(
            capability_id=CapabilityId(root="user.once"),
            version=CapabilityVersion(root="1.0.0"),
            kind=CapabilityKind.TOOL,
            name="One Shot Search",
            description="Search at most once per session.",
            transport=CapabilityTransport.BUILTIN_TOOL,
            runtime_endpoint="builtin.search",
            transport_config={"binary": "rg", "required": True},
            input_schema=SearchRequest.model_json_schema(),
            output_schema=SearchResult.model_json_schema(),
            install_source=CapabilityInstallSource(kind="local", location="/tmp/user.once"),
            owner="user",
            provenance="manual",
            risk_class=RiskClass.LOW,
            trust_tier=TrustTier.FOUNDATION,
            declared_side_effects=["filesystem_read"],
            constraints=CapabilityConstraintSet(
                path_rules=[
                    CapabilityScopeRule(
                        target=CapabilityScopeTarget.PATH,
                        kind=CapabilityScopeKind.WORKSPACE,
                    )
                ],
                network_rules=[
                    CapabilityScopeRule(
                        target=CapabilityScopeTarget.NETWORK,
                        kind=CapabilityScopeKind.NONE,
                    )
                ],
                side_effect_rules=[CapabilitySideEffectRule(side_effect="filesystem_read")],
                invocation_budget=CapabilityInvocationBudget(
                    timeout_seconds=30,
                    output_limit_kb=64,
                    max_invocations=1,
                ),
            ),
        )
    )
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="run_once",
        kind=ActionKind.TOOL_CALL,
        summary="Search once",
        tool_call=ToolCall(capability_id="user.once", arguments={"query": "needle"}),
    )

    first = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )
    assert first is not None
    assert first.verdict.outcome is CapabilityPolicyOutcome.ALLOW_WITH_CONSTRAINTS

    engine.register_invocation(first)

    second = engine.evaluate(
        action.model_copy(update={"id": "run_twice"}),
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert second is not None
    assert second.verdict.outcome is CapabilityPolicyOutcome.BLOCK
    assert second.verdict.reason_codes == [PolicyReasonCode.INVOCATION_LIMIT_EXCEEDED]


def test_approval_service_builds_capability_aware_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, workspace_root = _registry(tmp_path, monkeypatch)
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="touch_file",
        kind=ActionKind.SHELL,
        summary="Create a file",
        shell=ShellAction(command="touch", args=["note.txt"]),
    )
    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
    )

    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.REQUIRE_APPROVAL

    request = ApprovalService(mode=ApprovalMode.MANUAL).build_request(
        action,
        evaluation,
        request_cwd=workspace_root,
    )

    assert request.capability_id == "foundation.shell.command"
    assert request.risk_class is RiskClass.MEDIUM
    assert "workspace_write" in request.requested_side_effects
    assert request.constraints is not None
    assert request.constraints.invocation_budget is not None


def test_auto_except_commit_approves_workspace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation.models.history import ApprovalDecisionStatus

    registry, workspace_root = _registry(tmp_path, monkeypatch)
    engine = CapabilityPolicyEngine(
        workspace_root=workspace_root,
        capability_registry=registry,
    )
    action = PlannedAction(
        id="touch_file",
        kind=ActionKind.SHELL,
        summary="Create a file",
        shell=ShellAction(command="touch", args=["note.txt"]),
    )
    evaluation = engine.evaluate(
        action,
        request_cwd=workspace_root,
        approval_mode=ApprovalMode.AUTO_EXCEPT_COMMIT,
    )
    assert evaluation is not None
    assert evaluation.verdict.outcome is CapabilityPolicyOutcome.REQUIRE_APPROVAL

    _request, resolution = ApprovalService(mode=ApprovalMode.AUTO_EXCEPT_COMMIT).resolve(
        action, evaluation, request_cwd=workspace_root
    )

    assert resolution.status is ApprovalDecisionStatus.APPROVED
    assert "auto-except-commit" in resolution.reason


def _make_approval_request(
    *,
    capability_id: str,
    side_effects: list[str],
    network_hosts: list[str] | None = None,
) -> CapabilityApprovalRequest:
    return CapabilityApprovalRequest(
        action_id="a",
        capability_id=capability_id,
        capability_version="1.0.0",
        summary="test action",
        reason="test",
        risk_categories=[],
        risk_class=RiskClass.MEDIUM,
        trust_tier=TrustTier.FOUNDATION,
        reason_codes=[],
        cwd="/workspace",
        paths=[],
        network_hosts=network_hosts or [],
        requested_side_effects=side_effects,
    )


def test_auto_except_commit_gated_helper_detects_commit_capability() -> None:
    from foundation.services.approval import _auto_except_commit_gated

    request = _make_approval_request(
        capability_id="foundation.git.commit",
        side_effects=["workspace_write"],
    )
    assert _auto_except_commit_gated(request) == ("commit capability requires explicit approval")


def test_auto_except_commit_gated_helper_detects_network_side_effect() -> None:
    from foundation.services.approval import _auto_except_commit_gated

    request = _make_approval_request(
        capability_id="foundation.shell.command",
        side_effects=["network"],
        network_hosts=["example.com"],
    )
    gated = _auto_except_commit_gated(request)
    assert gated is not None
    assert "network" in gated


def test_auto_except_commit_gated_helper_detects_outside_workspace() -> None:
    from foundation.services.approval import _auto_except_commit_gated

    request = _make_approval_request(
        capability_id="foundation.shell.command",
        side_effects=["outside_workspace"],
    )
    gated = _auto_except_commit_gated(request)
    assert gated is not None
    assert "outside_workspace" in gated


def test_auto_except_commit_gated_helper_passes_workspace_write() -> None:
    from foundation.services.approval import _auto_except_commit_gated

    request = _make_approval_request(
        capability_id="foundation.file.write",
        side_effects=["workspace_write"],
    )
    assert _auto_except_commit_gated(request) is None


def test_git_mutation_subcommands_have_single_source_of_truth() -> None:
    """Planner and guardrails must share one definition (hardening stage 3)."""
    from foundation.models.git import GIT_MUTATION_SUBCOMMANDS
    from foundation.services import guardrails, planner

    assert planner._GIT_MUTATION_SUBCOMMANDS is GIT_MUTATION_SUBCOMMANDS
    assert guardrails._WRITE_GIT_SUBCOMMANDS is GIT_MUTATION_SUBCOMMANDS
