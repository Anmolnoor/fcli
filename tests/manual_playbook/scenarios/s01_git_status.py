"""S1 — summarize current git status.

Correctness-first grading: the assistant must identify the real branch
name, the real modified files, and the real recent commits. Using the
typed ``foundation.git.status`` capability is preferred (asserted as a
soft advisory), but a zero-action grounded answer is still a pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.conftest import init_git_repo
from tests.manual_playbook.graders import (
    GradeContext,
    GradeOutcome,
    Grader,
    assert_capability_used,
    assert_covers_behaviors,
)
from tests.manual_playbook.provider_stubs import (
    provider_response,
    zero_action_response,
)
from tests.manual_playbook.scenarios._base import Scenario

from foundation.models import ProviderResponse

PROMPT = "Summarize the current git status for this repo."


def setup(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    del monkeypatch  # unused for S1
    (workspace / "README.md").write_text(
        "# playbook repo\n",
        encoding="utf-8",
    )
    init_git_repo(workspace)

    notes = workspace / "notes.md"
    notes.write_text("pending thought\n", encoding="utf-8")

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout

    git("add", "notes.md")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    return {"branch": branch, "staged": ["notes.md"]}


def stub_responses(workspace: Path) -> list[ProviderResponse]:
    # Iteration 1: one foundation.git.status call.
    plan = {
        "assistant_message": "Reading git status.",
        "actions": [
            {
                "id": "status",
                "kind": "tool_call",
                "summary": "Check git status",
                "tool_call": {
                    "capability_id": "foundation.git.status",
                    "arguments": {},
                },
            }
        ],
    }
    # Iteration 2: zero actions, final grounded summary.
    summary = zero_action_response(
        "Branch main, with notes.md staged and no other pending changes. "
        "Most recent commit: initial commit."
    )
    return [provider_response(plan), summary]


def _grade_summary_mentions_branch_and_staged_file() -> Grader:
    return assert_covers_behaviors(
        {
            "branch_name": ["main", "branch main"],
            "staged_file": ["notes.md"],
            "recent_commit": ["initial commit", "most recent commit", "commit"],
        }
    )


def _grade_prefers_typed_git_status() -> Grader:
    # Soft preference: required=0 means this is always-pass; use the stronger
    # assertion that if any action was planned, it was the typed capability.
    base = assert_capability_used("foundation.git.status", at_least=1)

    def _grader(ctx: GradeContext) -> GradeOutcome:
        total_actions = sum(len(it.plan.actions) for it in ctx.result.iterations)
        if total_actions == 0:
            # Zero-action grounded answers are explicitly allowed by S1.
            return GradeOutcome(
                name="prefers_typed_git_status",
                passed=True,
                reason="zero-action grounded answer accepted",
                severity="warning",
            )
        return base(ctx)

    return _grader


SCENARIO = Scenario(
    name="s01_git_status",
    prompt=PROMPT,
    setup=setup,
    stub_responses=stub_responses,
    graders=[
        _grade_summary_mentions_branch_and_staged_file(),
        _grade_prefers_typed_git_status(),
    ],
)
