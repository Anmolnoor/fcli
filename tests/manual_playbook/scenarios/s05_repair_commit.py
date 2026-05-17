"""S5 — repair failing repo, stage, and stop for commit approval.

Exercises the full coding-turn contract: write a fix via the typed file
capability, run verification, stage the change, then request commit with
``requires_approval=True``. Under ``AUTO_EXCEPT_COMMIT`` the commit lands in
PENDING and the loop stops cleanly.

Graders correspond to the plan's five-part S5 rubric:
    A) target file content is the fixed version
    B) staged set matches the intended paths exactly
    C) final iteration planned the commit with requires_approval=True
    D) summary.pending_approval_actions == 1 AND session_status PENDING_APPROVAL
    E) no ``cat``/``grep``/``printf`` shell actions
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.conftest import init_git_repo
from tests.manual_playbook.graders import (
    GradeContext,
    GradeOutcome,
    Grader,
    assert_no_shell_equivalent,
)
from tests.manual_playbook.provider_stubs import provider_response, zero_action_response
from tests.manual_playbook.scenarios._base import Scenario

from foundation.models import ProviderResponse, SessionStatus
from foundation.models.git import GitStatusRequest
from foundation.services.git_service import GitService
from foundation.settings import ApprovalMode

PROMPT = (
    "Fix the failing test, run verification, stage the required changes, "
    "and stop for commit approval."
)

_FIXED_CONTENT = 'def hello() -> str:\n    return "hello"\n'


def setup(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Repo layout: minimal Python package with a failing test.
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "__init__.py").write_text(
        'def hello() -> str:\n    return "world"\n',
        encoding="utf-8",
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_hello.py").write_text(
        textwrap.dedent(
            """
            from pkg import hello


            def test_hello():
                assert hello() == "hello"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'pkg'\nversion = '0.0.1'\n",
        encoding="utf-8",
    )

    init_git_repo(workspace)

    # Install a wrapper ``pytest`` that exits 0 (the "fix" is good).
    bin_dir = workspace / ".bin"
    bin_dir.mkdir()
    shim = bin_dir / "pytest"
    shim.write_text(
        f"#!{sys.executable}\nimport sys\nsys.exit(0)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    return {"fixed_content": _FIXED_CONTENT}


def stub_responses(workspace: Path) -> list[ProviderResponse]:
    target = workspace / "src" / "pkg" / "__init__.py"
    plan = {
        "assistant_message": "Fixing the greeting and preparing the commit.",
        "actions": [
            {
                "id": "edit_source",
                "kind": "tool_call",
                "summary": "Rewrite pkg/__init__.py with the correct greeting",
                "tool_call": {
                    "capability_id": "foundation.file.write",
                    "arguments": {
                        "path": str(target),
                        "content": _FIXED_CONTENT,
                        "overwrite": True,
                    },
                },
            },
            {
                "id": "verify",
                "kind": "shell",
                "summary": "Run the test suite",
                "shell": {"command": "pytest", "args": ["-q"]},
            },
            {
                "id": "stage_fix",
                "kind": "tool_call",
                "summary": "Stage the edited file",
                "tool_call": {
                    "capability_id": "foundation.git.stage",
                    "arguments": {"paths": ["src/pkg/__init__.py"]},
                },
            },
            {
                "id": "commit_fix",
                "kind": "tool_call",
                "summary": "Commit the fix (awaiting approval)",
                "requires_approval": True,
                "approval_reason": "commit requires explicit approval",
                "tool_call": {
                    "capability_id": "foundation.git.commit",
                    "arguments": {"message": "fix: correct greeting"},
                },
            },
        ],
    }
    # Fallback summary for any unexpected extra iteration.
    return [provider_response(plan), zero_action_response("Awaiting commit approval.")]


def _grade_fixed_file_content() -> Grader:
    def _grader(ctx: GradeContext) -> GradeOutcome:
        target = ctx.workspace_root / "src" / "pkg" / "__init__.py"
        actual = target.read_text(encoding="utf-8") if target.exists() else ""
        expected = ctx.artifacts.get("fixed_content", _FIXED_CONTENT)
        if actual != expected:
            return GradeOutcome(
                name="fixed_file_content",
                passed=False,
                reason=f"target file content mismatch: {actual!r} != {expected!r}",
            )
        return GradeOutcome(name="fixed_file_content", passed=True)

    return _grader


def _grade_staged_set() -> Grader:
    def _grader(ctx: GradeContext) -> GradeOutcome:
        service = GitService(workspace_root=ctx.workspace_root)
        status = service.status(GitStatusRequest())
        staged = sorted(change.path for change in status.staged)
        expected = ["src/pkg/__init__.py"]
        if staged != expected:
            return GradeOutcome(
                name="staged_set",
                passed=False,
                reason=f"expected {expected}, got {staged}",
            )
        return GradeOutcome(name="staged_set", passed=True)

    return _grader


def _grade_commit_action_planned_with_approval() -> Grader:
    def _grader(ctx: GradeContext) -> GradeOutcome:
        for iteration in ctx.result.iterations:
            for action in iteration.plan.actions:
                if (
                    action.tool_call is not None
                    and action.tool_call.capability_id == "foundation.git.commit"
                    and action.requires_approval
                ):
                    return GradeOutcome(name="commit_action_with_approval", passed=True)
        return GradeOutcome(
            name="commit_action_with_approval",
            passed=False,
            reason="no foundation.git.commit action with requires_approval=True was planned",
        )

    return _grader


def _grade_pending_and_session_status() -> Grader:
    def _grader(ctx: GradeContext) -> GradeOutcome:
        pending = ctx.result.summary.pending_approval_actions
        if pending != 1:
            return GradeOutcome(
                name="pending_and_session_status",
                passed=False,
                reason=f"expected pending_approval_actions=1, got {pending}",
            )
        if ctx.session_status is not SessionStatus.PENDING_APPROVAL:
            return GradeOutcome(
                name="pending_and_session_status",
                passed=False,
                reason=f"expected PENDING_APPROVAL, got {ctx.session_status.value}",
            )
        return GradeOutcome(name="pending_and_session_status", passed=True)

    return _grader


SCENARIO = Scenario(
    name="s05_repair_commit",
    prompt=PROMPT,
    setup=setup,
    stub_responses=stub_responses,
    approval_mode=ApprovalMode.AUTO_EXCEPT_COMMIT,
    graders=[
        _grade_fixed_file_content(),
        _grade_staged_set(),
        _grade_commit_action_planned_with_approval(),
        _grade_pending_and_session_status(),
        assert_no_shell_equivalent({"cat", "grep", "printf"}),
    ],
)
