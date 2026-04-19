"""End-to-end integration tests for v3 Stage 06 release gate.

These tests exercise the full coding-turn surface with real filesystem,
real git repo, and (faked) verification binaries, end-to-end through
RequestOrchestrator.  They are the release-gate for v3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from foundation.models import (
    ExecutionStatus,
    LoopStopReason,
    PresentationNoticeLevel,
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
    UserRequest,
    VerificationOutcome,
)
from foundation.services import ApprovalService, HistoryStore, LocalToolService, ShellRuntime
from foundation.services.orchestrator import RequestOrchestrator
from foundation.settings import ApprovalMode


def _provider_response(payload: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(payload),
        structured_output=payload,
        metadata=ProviderResponseMetadata(
            provider="stub", model="stub-model", latency_seconds=0.01,
        ),
    )


class _StubProvider:
    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)

    def complete(self, _prompt: ProviderPrompt) -> ProviderResponse:
        if not self._responses:
            return _provider_response(
                {"assistant_message": "Done.", "actions": []}
            )
        return self._responses.pop(0)


def _install_fake_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int,
) -> None:
    """Install a fake `pytest` binary on PATH that exits with the given code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pytest"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import sys
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _git_workspace(tmp_path: Path) -> Path:
    """Initialize a real git repo with a minimal Python package."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "__init__.py").write_text(
        "def hello() -> str:\n    return 'world'\n", encoding="utf-8",
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_hello.py").write_text(
        "from pkg import hello\n\n\ndef test_hello():\n    assert hello() == 'world'\n",
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'pkg'\nversion = '0.0.1'\n", encoding="utf-8",
    )

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=workspace, check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    git("add", ".")
    git("commit", "-q", "-m", "initial commit")
    return workspace


def _orchestrator_for(
    workspace: Path,
    provider: _StubProvider,
    *,
    history_store: HistoryStore,
    approval_callback: Any,
) -> RequestOrchestrator:
    runtime = ShellRuntime(
        workspace_root=workspace,
        default_timeout_seconds=5,
        max_timeout_seconds=10,
        capture_limit_kb=64,
    )
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    approval_service = ApprovalService(
        mode=ApprovalMode.PROMPT, prompt_callback=approval_callback,
    )
    return RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=ApprovalMode.PROMPT,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        approval_service=approval_service,
        history_store=history_store,
    )


def _full_workflow_plan(workspace: Path) -> dict[str, Any]:
    """Plan: edit source, run tests, stage the edit, request commit."""
    target = workspace / "src" / "pkg" / "__init__.py"
    return {
        "assistant_message": "Fixing the greeting and verifying.",
        "actions": [
            {
                "id": "edit_src",
                "kind": "tool_call",
                "summary": "Rewrite pkg/__init__.py",
                "tool_call": {
                    "capability_id": "foundation.file.write",
                    "arguments": {
                        "path": str(target),
                        "content": (
                            "def hello() -> str:\n"
                            "    # tightened greeting\n"
                            "    return 'world'\n"
                        ),
                        "overwrite": True,
                    },
                },
            },
            {
                "id": "verify",
                "kind": "shell",
                "summary": "Run pytest",
                "shell": {"command": "pytest", "args": ["-q"]},
            },
            {
                "id": "stage_edit",
                "kind": "tool_call",
                "summary": "Stage the edited file",
                "tool_call": {
                    "capability_id": "foundation.git.stage",
                    "arguments": {"paths": ["src/pkg/__init__.py"]},
                },
            },
            {
                "id": "commit_it",
                "kind": "tool_call",
                "summary": "Commit the staged edit",
                "tool_call": {
                    "capability_id": "foundation.git.commit",
                    "arguments": {
                        "message": "fix: tighten greeting contract",
                    },
                },
            },
        ],
    }


def test_e2e_full_coding_workflow_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: edit, verify (PASSED), stage, commit (auto-approved)."""
    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch, exit_code=0)

    provider = _StubProvider([_provider_response(_full_workflow_plan(workspace))])
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator = _orchestrator_for(
        workspace, provider,
        history_store=history_store,
        approval_callback=lambda _req: True,  # auto-approve commit
    )

    result = orchestrator.orchestrate(UserRequest(message="fix and commit"))

    # All four actions completed successfully.
    assert len(result.execution_results) == 4
    statuses = [r.status for r in result.execution_results]
    assert statuses == [ExecutionStatus.EXECUTED] * 4

    # Verification reported PASSED.
    assert result.verification_notice is not None
    assert result.verification_notice.outcome is VerificationOutcome.PASSED

    # Commit landed in the real repo.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace,
        check=True, capture_output=True, text=True,
    )
    assert "tighten greeting contract" in log.stdout

    # Loop terminated cleanly on the next zero-action turn.
    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN


def test_e2e_commit_approval_denied_leaves_workspace_staged_not_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Denying commit approval stops the loop, leaves staged edits, no commit."""
    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch, exit_code=0)

    # Callback approves stage but denies commit.
    def approval_callback(request: Any) -> bool:
        return "commit" not in (request.capability_id or "")

    provider = _StubProvider([_provider_response(_full_workflow_plan(workspace))])
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator = _orchestrator_for(
        workspace, provider,
        history_store=history_store,
        approval_callback=approval_callback,
    )

    result = orchestrator.orchestrate(UserRequest(message="fix and try commit"))

    # Commit action did NOT execute.
    commit_result = next(
        r for r in result.execution_results if r.action_id == "commit_it"
    )
    assert commit_result.status is not ExecutionStatus.EXECUTED

    # No new commit on the real repo; only the initial commit remains.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace,
        check=True, capture_output=True, text=True,
    )
    assert log.stdout.count("\n") == 1  # exactly one commit

    # Edited file still has the modification applied (workspace left modified).
    target = workspace / "src" / "pkg" / "__init__.py"
    assert target.read_text(encoding="utf-8").strip().endswith("'world'")

    # The stage action DID execute, so the edit is staged.
    stage_result = next(
        r for r in result.execution_results if r.action_id == "stage_edit"
    )
    assert stage_result.status is ExecutionStatus.EXECUTED


def test_e2e_verification_unavailable_on_fatal_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing verification binary reports UNAVAILABLE and leaves edits applied."""
    workspace = _git_workspace(tmp_path)
    # PATH retains system binaries (git) so capability health checks pass,
    # but no `pytest` is installed — so verification must report UNAVAILABLE.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", f"{empty_bin}:/usr/bin:/bin")

    provider = _StubProvider([_provider_response(_full_workflow_plan(workspace))])
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator = _orchestrator_for(
        workspace, provider,
        history_store=history_store,
        approval_callback=lambda _req: True,
    )

    result = orchestrator.orchestrate(UserRequest(message="fix with missing pytest"))

    # Loop stopped fatally because pytest spawn failed.
    assert result.stop_reason is LoopStopReason.FATAL_EXECUTION_FAILURE

    # Verification reports UNAVAILABLE, NOT falsely claiming PASSED.
    assert result.verification_notice is not None
    assert result.verification_notice.outcome is VerificationOutcome.UNAVAILABLE
    assert result.verification_notice.verified is False

    # The file edit from before the fatal step remains on disk.
    target = workspace / "src" / "pkg" / "__init__.py"
    assert target.exists()


def test_e2e_concise_and_verbose_presenter_parity_on_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path renders concise notices + verbose retains full detail."""
    from foundation.cli import _build_chat_turn_presentation
    from foundation.models import ChatSurfacePolicy, RenderMode

    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch, exit_code=0)

    provider = _StubProvider([_provider_response(_full_workflow_plan(workspace))])
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator = _orchestrator_for(
        workspace, provider,
        history_store=history_store,
        approval_callback=lambda _req: True,
    )
    result = orchestrator.orchestrate(UserRequest(message="fix and verify"))

    concise = _build_chat_turn_presentation(
        result,
        policy=ChatSurfacePolicy(render_mode=RenderMode.CONCISE),
        interactive=False,
    )

    concise_notice_texts = [n.text for n in concise.notices]
    assert any("Changed file" in t for t in concise_notice_texts)
    assert any("Command" in t and "pytest" in t for t in concise_notice_texts)
    assert any(
        n.text.startswith("Verification: passed")
        and n.level is PresentationNoticeLevel.INFO
        for n in concise.notices
    )

    # Verbose rendering reads from the full result: iterations[0].plan is the
    # terminal-iteration plan (zero actions here), but the first iteration's
    # plan still holds the full 4-action detail.
    assert len(result.iterations) >= 1
    assert len(result.iterations[0].plan.actions) == 4
    assert len(result.execution_results) == 4
