from __future__ import annotations

import json
import sqlite3
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


def test_schema_v5_migration_rewrites_replan_edges_and_loads_old_steps(
    tmp_path: Path,
) -> None:
    """v4 DB with REPLAN edges and steps lacking iteration_index upgrades cleanly."""
    database_path = tmp_path / "history.sqlite3"

    # Build a v4-shaped DB by running the current schema script then forcing
    # user_version back to 4 and inserting legacy rows.
    HistoryStore(database_path=database_path)  # creates fresh schema @ v5
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA user_version = 4")
        connection.execute(
            "INSERT INTO sessions (id, kind, status, workspace_root, request_cwd, "
            "approval_mode, plan_only, command_preview, started_at) "
            "VALUES ('sess-legacy', 'chat', 'completed', '/ws', '/ws', 'prompt', 0, "
            "'legacy', '2026-01-01T00:00:00Z')"
        )
        legacy_planning = {
            "step_type": "planning",
            "step_id": "planning:req-legacy",
            "trace_id": "sess-legacy",
            "session_id": "sess-legacy",
            "request_id": "req-legacy",
            "request_text": "do thing",
            "request_cwd": "/ws",
            "candidate_capability_ids": [],
            "selection_reasons": [],
            "action_ids": ["a1"],
            "planning_metadata": {
                "provider": "stub",
                "model": "stub-model",
                "response_id": None,
                "latency_seconds": 0.0,
                "attempts": 1,
                "usage": None,
            },
            "artifacts": [],
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:00Z",
            "duration_seconds": 0.0,
        }
        connection.execute(
            "INSERT INTO trace_steps (session_id, trace_id, step_id, step_type, "
            "action_id, capability_id, capability_version, status, record_json, "
            "created_at) "
            "VALUES ('sess-legacy', 'sess-legacy', 'planning:req-legacy', 'planning', "
            "NULL, NULL, NULL, NULL, ?, '2026-01-01T00:00:00Z')",
            (json.dumps(legacy_planning),),
        )
        connection.execute(
            "INSERT INTO trace_edges (session_id, trace_id, source_step_id, "
            "target_step_id, edge_kind, created_at) "
            "VALUES ('sess-legacy', 'sess-legacy', 'action:old', "
            "'planning:next', 'replan', '2026-01-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()

    # Re-open: triggers _ensure_schema → _migrate_to_v5
    HistoryStore(database_path=database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == 6
        edges = connection.execute(
            "SELECT edge_kind FROM trace_edges WHERE session_id = 'sess-legacy'"
        ).fetchall()
        assert [row["edge_kind"] for row in edges] == ["replanned_from"]
    finally:
        connection.close()

    # Legacy step loads with default iteration_index=1
    from foundation.models import TraceQuery

    trace = HistoryStore(database_path=database_path).get_trace(
        TraceQuery(session_id="sess-legacy")
    )
    assert trace is not None
    assert len(trace.steps) == 1
    legacy_step = trace.steps[0]
    assert legacy_step.step_type.value == "planning"
    assert legacy_step.iteration_index == 1


def test_schema_v6_migration_keys_assistant_plans_per_iteration(
    tmp_path: Path,
) -> None:
    """A v5 database with the legacy ``UNIQUE(session_id)`` shape rebuilds
    cleanly under v6 with ``UNIQUE(session_id, iteration)`` and preserves
    every per-iteration plan row.
    """
    database_path = tmp_path / "history.sqlite3"

    # Build a v5-shaped DB by hand with the *old* unique constraint.
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_root TEXT NOT NULL,
                request_cwd TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                plan_only INTEGER NOT NULL DEFAULT 0,
                command_preview TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE assistant_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                iteration INTEGER NOT NULL DEFAULT 1,
                assistant_message TEXT NOT NULL,
                context_json TEXT,
                plan_json TEXT NOT NULL,
                planning_metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id)
            );
            INSERT INTO sessions (id, kind, status, workspace_root, request_cwd,
                                  approval_mode, started_at)
            VALUES ('sess-legacy', 'chat', 'completed', '/ws', '/ws', 'prompt',
                    '2026-01-01T00:00:00Z');
            INSERT INTO assistant_plans (
                session_id, iteration, assistant_message, plan_json,
                planning_metadata_json, created_at
            ) VALUES (
                'sess-legacy', 1, 'iter-1', '{}', '{}', '2026-01-01T00:00:00Z'
            );
            """
        )
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    # Re-open: should run _migrate_to_v6 and rebuild the table.
    HistoryStore(database_path=database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        # Existing row preserved.
        rows = connection.execute(
            "SELECT iteration, assistant_message FROM assistant_plans "
            "WHERE session_id = 'sess-legacy' ORDER BY iteration"
        ).fetchall()
        assert [row["assistant_message"] for row in rows] == ["iter-1"]
        # New per-iteration constraint accepts a second row.
        connection.execute(
            "INSERT INTO assistant_plans (session_id, iteration, "
            "assistant_message, plan_json, planning_metadata_json, created_at) "
            "VALUES ('sess-legacy', 2, 'iter-2', '{}', '{}', "
            "'2026-01-01T00:00:01Z')"
        )
        connection.commit()
        rows = connection.execute(
            "SELECT iteration FROM assistant_plans "
            "WHERE session_id = 'sess-legacy' ORDER BY iteration"
        ).fetchall()
        assert [row["iteration"] for row in rows] == [1, 2]
    finally:
        connection.close()


def _build_v5_database(database_path: Path, *, duplicate_iteration_rows: bool = False) -> None:
    """Create a v5-shaped DB with the legacy assistant_plans constraint.

    With ``duplicate_iteration_rows`` the table is built without any unique
    constraint and seeded with two rows sharing (session_id, iteration) — a
    corrupt shape the v6 rebuild must refuse to destroy (hardening stage 5).
    """
    constraint = "" if duplicate_iteration_rows else ", UNIQUE(session_id)"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_root TEXT NOT NULL,
                request_cwd TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                plan_only INTEGER NOT NULL DEFAULT 0,
                command_preview TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE assistant_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                iteration INTEGER NOT NULL DEFAULT 1,
                assistant_message TEXT NOT NULL,
                context_json TEXT,
                plan_json TEXT NOT NULL,
                planning_metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL{constraint}
            );
            INSERT INTO sessions (id, kind, status, workspace_root, request_cwd,
                                  approval_mode, started_at)
            VALUES ('sess-legacy', 'chat', 'completed', '/ws', '/ws', 'prompt',
                    '2026-01-01T00:00:00Z');
            INSERT INTO assistant_plans (
                session_id, iteration, assistant_message, plan_json,
                planning_metadata_json, created_at
            ) VALUES (
                'sess-legacy', 1, 'iter-1', '{{}}', '{{}}', '2026-01-01T00:00:00Z'
            );
            """
        )
        if duplicate_iteration_rows:
            connection.execute(
                "INSERT INTO assistant_plans (session_id, iteration, "
                "assistant_message, plan_json, planning_metadata_json, created_at) "
                "VALUES ('sess-legacy', 1, 'iter-1-dup', '{}', '{}', "
                "'2026-01-01T00:00:01Z')"
            )
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()


def test_migration_writes_backup_before_running(tmp_path: Path) -> None:
    """Hardening stage 5: a schema migration backs up the DB file first."""
    database_path = tmp_path / "history.sqlite3"
    _build_v5_database(database_path)

    HistoryStore(database_path=database_path)

    backup_path = tmp_path / "history.sqlite3.pre-v6.bak"
    assert backup_path.exists()
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        count = backup.execute("SELECT COUNT(*) FROM assistant_plans").fetchone()[0]
        assert count == 1
    finally:
        backup.close()


def test_sabotaged_v6_rebuild_raises_and_preserves_original(tmp_path: Path) -> None:
    """Hardening stage 5: a failing rebuild must not destroy history."""
    import pytest

    from foundation.services.history import HistoryMigrationError

    database_path = tmp_path / "history.sqlite3"
    _build_v5_database(database_path, duplicate_iteration_rows=True)

    with pytest.raises(HistoryMigrationError, match="pre-v6.bak"):
        HistoryStore(database_path=database_path)

    # Original database untouched and readable at the old version.
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        count = connection.execute("SELECT COUNT(*) FROM assistant_plans").fetchone()[0]
        assert count == 2
    finally:
        connection.close()
    assert (tmp_path / "history.sqlite3.pre-v6.bak").exists()
