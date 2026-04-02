from __future__ import annotations

from pathlib import Path

from foundation.models import SessionKind, SessionStatus
from foundation.services import HistoryStore, WorkspaceRewriteStager


def test_history_store_truncates_large_command_output(tmp_path: Path) -> None:
    history_store = HistoryStore(
        database_path=tmp_path / "history.sqlite3",
        max_blob_bytes=128,
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    session_id = history_store.start_session(
        kind=SessionKind.RUN,
        workspace_root=workspace_root,
        request_cwd=workspace_root,
        approval_mode="prompt",
        command_preview="python -c ...",
    )

    history_store.record_command(
        session_id,
        action_id=None,
        source="cli.run",
        command="python",
        args=["-c", "print('x')"],
        cwd=str(workspace_root),
        mode="buffered",
        policy_decision="allow",
        policy_reason="direct run",
        risk_categories=[],
        execution_status="executed",
        exit_code=0,
        duration_seconds=0.01,
        stdout="a" * 1024,
        stderr="",
        error=None,
    )
    history_store.record_summary(
        session_id,
        assistant_message=None,
        summary_text="Executed shell command.",
        executed_actions=1,
        pending_approval_actions=0,
        blocked_actions=0,
        failed_actions=0,
        skipped_actions=0,
    )
    history_store.finalize_session(session_id, status=SessionStatus.COMPLETED)

    detail = history_store.get_session(session_id)
    assert detail is not None
    assert detail.commands[0].stdout_truncated is True
    assert len(detail.commands[0].stdout) < 1024


def test_workspace_rewrite_stager_stages_before_commit(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    workspace_root.mkdir()
    state_dir.mkdir()
    stager = WorkspaceRewriteStager(workspace_root=workspace_root, state_dir=state_dir)

    staged = stager.stage_text(target_path=Path("notes.txt"), content="hello\n")

    target_path = workspace_root / "notes.txt"
    assert not target_path.exists()
    assert Path(staged.staged_path).exists()

    stager.commit(staged)

    assert target_path.read_text(encoding="utf-8") == "hello\n"
    assert not Path(staged.staged_path).exists()
