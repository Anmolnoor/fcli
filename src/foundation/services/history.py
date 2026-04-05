"""SQLite-backed Stage 6 history and audit persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from foundation.models.history import (
    ApprovalRequest,
    ApprovalResolution,
    HistoryApprovalRecord,
    HistoryCommandRecord,
    HistoryEventRecord,
    HistorySessionDetail,
    HistorySessionSummary,
    HistoryToolCallRecord,
    SessionKind,
    SessionStatus,
)

logger = logging.getLogger("foundation.services.history")

_SCHEMA_VERSION = 1
_DEFAULT_MAX_BLOB_BYTES = 64 * 1024

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    request_cwd TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    plan_only INTEGER NOT NULL DEFAULT 0,
    command_preview TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS user_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
    assistant_message TEXT NOT NULL,
    context_json TEXT,
    plan_json TEXT NOT NULL,
    planning_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    policy_decision TEXT,
    policy_reason TEXT,
    risk_categories_json TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    artifact_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executed_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id TEXT,
    source TEXT NOT NULL,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL,
    cwd TEXT,
    mode TEXT,
    policy_decision TEXT,
    policy_reason TEXT,
    risk_categories_json TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    stdout_truncated INTEGER NOT NULL DEFAULT 0,
    stderr_truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk_categories_json TEXT NOT NULL,
    command_preview TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summarized_outcomes (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    assistant_message TEXT,
    summary_text TEXT NOT NULL,
    executed_actions INTEGER NOT NULL,
    pending_approval_actions INTEGER NOT NULL,
    blocked_actions INTEGER NOT NULL,
    failed_actions INTEGER NOT NULL,
    skipped_actions INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_id ON tool_calls(session_id, id);
CREATE INDEX IF NOT EXISTS idx_commands_session_id ON executed_commands(session_id, id);
CREATE INDEX IF NOT EXISTS idx_approvals_session_id ON approval_decisions(session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
"""


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


class HistoryStore:
    """Record and query Stage 6 session history."""

    def __init__(
        self,
        *,
        database_path: Path,
        retention_days: int = 30,
        max_entries: int = 5000,
        max_blob_bytes: int = _DEFAULT_MAX_BLOB_BYTES,
    ) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._retention_days = retention_days
        self._max_entries = max_entries
        self._max_blob_bytes = max_blob_bytes
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def database_path(self) -> Path:
        """Return the configured SQLite file path."""
        return self._database_path

    def start_session(
        self,
        *,
        kind: SessionKind,
        workspace_root: Path,
        request_cwd: Path,
        approval_mode: str,
        plan_only: bool = False,
        request_text: str | None = None,
        command_preview: str | None = None,
    ) -> str:
        session_id = uuid4().hex
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id,
                    kind,
                    status,
                    workspace_root,
                    request_cwd,
                    approval_mode,
                    plan_only,
                    command_preview,
                    started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    kind.value,
                    SessionStatus.COMPLETED.value,
                    str(Path(workspace_root).resolve()),
                    str(Path(request_cwd).resolve()),
                    approval_mode,
                    int(plan_only),
                    command_preview,
                    created_at,
                ),
            )
            if request_text:
                connection.execute(
                    """
                    INSERT INTO user_messages (session_id, content, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, request_text, created_at),
                )
        self.record_event(
            session_id,
            "session_started",
            {
                "kind": kind.value,
                "plan_only": plan_only,
                "approval_mode": approval_mode,
            },
        )
        return session_id

    def record_plan(
        self,
        session_id: str,
        *,
        assistant_message: str,
        context: dict[str, object],
        plan: dict[str, object],
        planning_metadata: dict[str, object],
    ) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO assistant_plans (
                    session_id,
                    assistant_message,
                    context_json,
                    plan_json,
                    planning_metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    assistant_message,
                    self._encode_json_blob(context),
                    self._encode_json_blob(plan),
                    self._encode_json_blob(planning_metadata),
                    created_at,
                ),
            )
        actions = plan.get("actions")
        action_count = len(actions) if isinstance(actions, list) else 0
        self.record_event(
            session_id,
            "plan_recorded",
            {"assistant_message": assistant_message, "action_count": action_count},
        )

    def record_tool_call(
        self,
        session_id: str,
        *,
        action_id: str,
        tool: str,
        arguments: dict[str, object],
        policy_decision: str | None,
        policy_reason: str | None,
        risk_categories: list[str],
        execution_status: str,
        artifact: dict[str, object] | None,
        error: str | None,
    ) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls (
                    session_id,
                    action_id,
                    tool,
                    arguments_json,
                    policy_decision,
                    policy_reason,
                    risk_categories_json,
                    execution_status,
                    artifact_json,
                    error,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    action_id,
                    tool,
                    self._encode_json_blob(arguments),
                    policy_decision,
                    policy_reason,
                    _json_dumps(risk_categories),
                    execution_status,
                    None if artifact is None else self._encode_json_blob(artifact),
                    error,
                    created_at,
                ),
            )
        self.record_event(
            session_id,
            "tool_call_recorded",
            {"action_id": action_id, "tool": tool, "execution_status": execution_status},
        )

    def record_command(
        self,
        session_id: str,
        *,
        action_id: str | None,
        source: str,
        command: str,
        args: list[str],
        cwd: str | None,
        mode: str | None,
        policy_decision: str | None,
        policy_reason: str | None,
        risk_categories: list[str],
        execution_status: str,
        exit_code: int | None = None,
        duration_seconds: float | None = None,
        stdout: str = "",
        stderr: str = "",
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        error: str | None = None,
    ) -> None:
        created_at = _utcnow()
        stored_stdout, stored_stdout_truncated = self._truncate_text(stdout)
        stored_stderr, stored_stderr_truncated = self._truncate_text(stderr)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO executed_commands (
                    session_id,
                    action_id,
                    source,
                    command,
                    args_json,
                    cwd,
                    mode,
                    policy_decision,
                    policy_reason,
                    risk_categories_json,
                    execution_status,
                    exit_code,
                    duration_seconds,
                    stdout,
                    stderr,
                    stdout_truncated,
                    stderr_truncated,
                    error,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    action_id,
                    source,
                    command,
                    _json_dumps(args),
                    cwd,
                    mode,
                    policy_decision,
                    policy_reason,
                    _json_dumps(risk_categories),
                    execution_status,
                    exit_code,
                    duration_seconds,
                    stored_stdout,
                    stored_stderr,
                    int(stdout_truncated or stored_stdout_truncated),
                    int(stderr_truncated or stored_stderr_truncated),
                    error,
                    created_at,
                ),
            )
        self.record_event(
            session_id,
            "command_recorded",
            {
                "action_id": action_id,
                "command": command,
                "execution_status": execution_status,
                "exit_code": exit_code,
            },
        )

    def record_approval(
        self,
        session_id: str,
        *,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_decisions (
                    session_id,
                    action_id,
                    mode,
                    status,
                    reason,
                    risk_categories_json,
                    command_preview,
                    requested_at,
                    resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    request.action_id,
                    resolution.mode,
                    resolution.status.value,
                    resolution.reason,
                    _json_dumps(request.risk_categories),
                    request.command_preview,
                    resolution.requested_at,
                    resolution.resolved_at,
                ),
            )
        self.record_event(
            session_id,
            "approval_recorded",
            {
                "action_id": request.action_id,
                "status": resolution.status.value,
                "mode": resolution.mode,
            },
        )

    def record_summary(
        self,
        session_id: str,
        *,
        assistant_message: str | None,
        summary_text: str,
        executed_actions: int,
        pending_approval_actions: int,
        blocked_actions: int,
        failed_actions: int,
        skipped_actions: int,
    ) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO summarized_outcomes (
                    session_id,
                    assistant_message,
                    summary_text,
                    executed_actions,
                    pending_approval_actions,
                    blocked_actions,
                    failed_actions,
                    skipped_actions,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    assistant_message,
                    summary_text,
                    executed_actions,
                    pending_approval_actions,
                    blocked_actions,
                    failed_actions,
                    skipped_actions,
                    created_at,
                ),
            )
        self.record_event(session_id, "summary_recorded", {"summary_text": summary_text})

    def record_event(self, session_id: str, event_type: str, payload: dict[str, object]) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (session_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, event_type, self._encode_json_blob(payload), created_at),
            )

    def finalize_session(self, session_id: str, *, status: SessionStatus) -> None:
        completed_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, completed_at = ?
                WHERE id = ?
                """,
                (status.value, completed_at, session_id),
            )
        self.record_event(session_id, "session_finished", {"status": status.value})
        self._prune()

    def list_sessions(self, *, limit: int = 20) -> list[HistorySessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    sessions.id,
                    sessions.kind,
                    sessions.status,
                    sessions.workspace_root,
                    sessions.request_cwd,
                    sessions.approval_mode,
                    sessions.plan_only,
                    sessions.command_preview,
                    sessions.started_at,
                    sessions.completed_at,
                    (
                        SELECT content
                        FROM user_messages
                        WHERE user_messages.session_id = sessions.id
                        ORDER BY user_messages.id ASC
                        LIMIT 1
                    ) AS request_text,
                    summarized_outcomes.summary_text,
                    COALESCE(summarized_outcomes.executed_actions, 0) AS executed_actions,
                    COALESCE(summarized_outcomes.pending_approval_actions, 0)
                        AS pending_approval_actions,
                    COALESCE(summarized_outcomes.blocked_actions, 0) AS blocked_actions,
                    COALESCE(summarized_outcomes.failed_actions, 0) AS failed_actions,
                    COALESCE(summarized_outcomes.skipped_actions, 0) AS skipped_actions
                FROM sessions
                LEFT JOIN summarized_outcomes
                    ON summarized_outcomes.session_id = sessions.id
                ORDER BY sessions.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def get_session(self, session_id: str) -> HistorySessionDetail | None:
        with self._connect() as connection:
            session_row = connection.execute(
                """
                SELECT
                    sessions.id,
                    sessions.kind,
                    sessions.status,
                    sessions.workspace_root,
                    sessions.request_cwd,
                    sessions.approval_mode,
                    sessions.plan_only,
                    sessions.command_preview,
                    sessions.started_at,
                    sessions.completed_at,
                    (
                        SELECT content
                        FROM user_messages
                        WHERE user_messages.session_id = sessions.id
                        ORDER BY user_messages.id ASC
                        LIMIT 1
                    ) AS request_text,
                    summarized_outcomes.summary_text,
                    COALESCE(summarized_outcomes.executed_actions, 0) AS executed_actions,
                    COALESCE(summarized_outcomes.pending_approval_actions, 0)
                        AS pending_approval_actions,
                    COALESCE(summarized_outcomes.blocked_actions, 0) AS blocked_actions,
                    COALESCE(summarized_outcomes.failed_actions, 0) AS failed_actions,
                    COALESCE(summarized_outcomes.skipped_actions, 0) AS skipped_actions,
                    summarized_outcomes.assistant_message AS summary_assistant_message,
                    assistant_plans.assistant_message AS plan_assistant_message,
                    assistant_plans.context_json,
                    assistant_plans.plan_json,
                    assistant_plans.planning_metadata_json
                FROM sessions
                LEFT JOIN summarized_outcomes
                    ON summarized_outcomes.session_id = sessions.id
                LEFT JOIN assistant_plans
                    ON assistant_plans.session_id = sessions.id
                WHERE sessions.id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None

            approvals = [
                HistoryApprovalRecord.model_validate(
                    {
                        "action_id": row["action_id"],
                        "mode": row["mode"],
                        "status": row["status"],
                        "reason": row["reason"],
                        "risk_categories": self._load_json_list(row["risk_categories_json"]),
                        "command_preview": row["command_preview"],
                        "requested_at": row["requested_at"],
                        "resolved_at": row["resolved_at"],
                    }
                )
                for row in connection.execute(
                    """
                    SELECT
                        action_id,
                        mode,
                        status,
                        reason,
                        risk_categories_json,
                        command_preview,
                        requested_at,
                        resolved_at
                    FROM approval_decisions
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
            ]

            tool_calls = [
                HistoryToolCallRecord.model_validate(
                    {
                        "action_id": row["action_id"],
                        "tool": row["tool"],
                        "arguments": self._load_json_dict(row["arguments_json"]),
                        "policy_decision": row["policy_decision"],
                        "policy_reason": row["policy_reason"],
                        "risk_categories": self._load_json_list(row["risk_categories_json"]),
                        "execution_status": row["execution_status"],
                        "artifact": self._load_json_dict(row["artifact_json"]),
                        "error": row["error"],
                        "created_at": row["created_at"],
                    }
                )
                for row in connection.execute(
                    """
                    SELECT
                        action_id,
                        tool,
                        arguments_json,
                        policy_decision,
                        policy_reason,
                        risk_categories_json,
                        execution_status,
                        artifact_json,
                        error,
                        created_at
                    FROM tool_calls
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
            ]

            commands = [
                HistoryCommandRecord.model_validate(
                    {
                        "action_id": row["action_id"],
                        "source": row["source"],
                        "command": row["command"],
                        "args": self._load_json_list(row["args_json"]),
                        "cwd": row["cwd"],
                        "mode": row["mode"],
                        "policy_decision": row["policy_decision"],
                        "policy_reason": row["policy_reason"],
                        "risk_categories": self._load_json_list(row["risk_categories_json"]),
                        "execution_status": row["execution_status"],
                        "exit_code": row["exit_code"],
                        "duration_seconds": row["duration_seconds"],
                        "stdout": row["stdout"],
                        "stderr": row["stderr"],
                        "stdout_truncated": bool(row["stdout_truncated"]),
                        "stderr_truncated": bool(row["stderr_truncated"]),
                        "error": row["error"],
                        "created_at": row["created_at"],
                    }
                )
                for row in connection.execute(
                    """
                    SELECT
                        action_id,
                        source,
                        command,
                        args_json,
                        cwd,
                        mode,
                        policy_decision,
                        policy_reason,
                        risk_categories_json,
                        execution_status,
                        exit_code,
                        duration_seconds,
                        stdout,
                        stderr,
                        stdout_truncated,
                        stderr_truncated,
                        error,
                        created_at
                    FROM executed_commands
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
            ]

            events = [
                HistoryEventRecord.model_validate(
                    {
                        "event_type": row["event_type"],
                        "payload": self._load_json_dict(row["payload_json"]) or {},
                        "created_at": row["created_at"],
                    }
                )
                for row in connection.execute(
                    """
                    SELECT event_type, payload_json, created_at
                    FROM events
                    WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
            ]

        summary = self._summary_from_row(session_row)
        assistant_message = (
            session_row["summary_assistant_message"] or session_row["plan_assistant_message"]
        )
        return HistorySessionDetail(
            **summary.model_dump(),
            assistant_message=assistant_message,
            context=self._load_json_dict(session_row["context_json"]),
            plan=self._load_json_dict(session_row["plan_json"]),
            planning_metadata=self._load_json_dict(session_row["planning_metadata_json"]),
            approvals=approvals,
            tool_calls=tool_calls,
            commands=commands,
            events=events,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _encode_json_blob(self, payload: object) -> str:
        raw = _json_dumps(payload)
        if len(raw.encode("utf-8")) <= self._max_blob_bytes:
            return raw
        preview_text, _ = self._truncate_text(raw)
        return _json_dumps(
            {
                "truncated": True,
                "original_size_bytes": len(raw.encode("utf-8")),
                "preview": preview_text,
            }
        )

    def _truncate_text(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= self._max_blob_bytes:
            return value, False
        truncated = encoded[: self._max_blob_bytes].decode("utf-8", errors="ignore")
        return truncated, True

    def _load_json_dict(self, raw: str | None) -> dict[str, object] | None:
        if raw is None:
            return None
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    def _load_json_list(self, raw: str | None) -> list[str]:
        if raw is None:
            return []
        payload = json.loads(raw)
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload]

    def _summary_from_row(self, row: sqlite3.Row) -> HistorySessionSummary:
        return HistorySessionSummary(
            session_id=row["id"],
            kind=row["kind"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_root=row["workspace_root"],
            request_cwd=row["request_cwd"],
            approval_mode=row["approval_mode"],
            plan_only=bool(row["plan_only"]),
            request_text=row["request_text"],
            command_preview=row["command_preview"],
            summary_text=row["summary_text"],
            executed_actions=row["executed_actions"],
            pending_approval_actions=row["pending_approval_actions"],
            blocked_actions=row["blocked_actions"],
            failed_actions=row["failed_actions"],
            skipped_actions=row["skipped_actions"],
        )

    def _prune(self) -> None:
        cutoff = (
            (datetime.now(tz=UTC).replace(microsecond=0) - timedelta(days=self._retention_days))
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE completed_at IS NOT NULL AND completed_at < ?",
                (cutoff,),
            )
            extra_ids = connection.execute(
                """
                SELECT id
                FROM sessions
                ORDER BY started_at DESC
                LIMIT -1 OFFSET ?
                """,
                (self._max_entries,),
            ).fetchall()
            for row in extra_ids:
                connection.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("history_pruned database_path=%s", self._database_path)
