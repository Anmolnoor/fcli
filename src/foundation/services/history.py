"""SQLite-backed Stage 6 history and audit persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from foundation.models import PolicyEvaluationRecord
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
from foundation.models.orchestration import ExecutionStatus
from foundation.models.trace import (
    AuditReport,
    ExecutionStep,
    PlanningStep,
    TraceEdge,
    TraceQuery,
    TraceRecord,
    TraceSummary,
)
from foundation.observability import redact_payload

logger = logging.getLogger("foundation.services.history")

_SCHEMA_VERSION = 5
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
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL DEFAULT 1,
    assistant_message TEXT NOT NULL,
    context_json TEXT,
    plan_json TEXT NOT NULL,
    planning_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, iteration)
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

CREATE TABLE IF NOT EXISTS capability_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id TEXT,
    capability_id TEXT,
    request_json TEXT NOT NULL,
    resolution_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    total_iterations INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    step_type TEXT NOT NULL,
    action_id TEXT,
    capability_id TEXT,
    capability_version TEXT,
    status TEXT,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, step_id)
);

CREATE TABLE IF NOT EXISTS trace_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL,
    source_step_id TEXT NOT NULL,
    target_step_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_id ON tool_calls(session_id, id);
CREATE INDEX IF NOT EXISTS idx_commands_session_id ON executed_commands(session_id, id);
CREATE INDEX IF NOT EXISTS idx_approvals_session_id ON approval_decisions(session_id, id);
CREATE INDEX IF NOT EXISTS idx_capability_approvals_session_id
    ON capability_approvals(session_id, id);
CREATE INDEX IF NOT EXISTS idx_policy_evaluations_session_id
    ON policy_evaluations(session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_trace_steps_session_id ON trace_steps(session_id, id);
CREATE INDEX IF NOT EXISTS idx_trace_steps_lookup ON trace_steps(session_id, step_id);
CREATE INDEX IF NOT EXISTS idx_trace_edges_session_id ON trace_edges(session_id, id);
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
        iteration: int = 1,
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
                    iteration,
                    assistant_message,
                    context_json,
                    plan_json,
                    planning_metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    iteration,
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
            {
                "assistant_message": assistant_message,
                "action_count": action_count,
                "iteration": iteration,
            },
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
        created_at = _utcnow()
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
            connection.execute(
                """
                INSERT INTO capability_approvals (
                    session_id,
                    action_id,
                    capability_id,
                    request_json,
                    resolution_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    request.action_id,
                    request.capability_id,
                    self._encode_json_blob(request.model_dump(mode="json")),
                    self._encode_json_blob(resolution.model_dump(mode="json")),
                    created_at,
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

    def record_policy_evaluation(
        self,
        session_id: str,
        *,
        record: PolicyEvaluationRecord,
    ) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO policy_evaluations (
                    session_id,
                    action_id,
                    capability_id,
                    outcome,
                    record_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    record.action_id,
                    record.capability_id,
                    record.verdict.outcome.value,
                    self._encode_json_blob(record.model_dump(mode="json")),
                    created_at,
                ),
            )
        self.record_event(
            session_id,
            "policy_evaluation_recorded",
            {
                "action_id": record.action_id,
                "capability_id": record.capability_id,
                "outcome": record.verdict.outcome.value,
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
        total_iterations: int = 1,
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
                    total_iterations,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    total_iterations,
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
                (
                    session_id,
                    event_type,
                    self._encode_json_blob(redact_payload(payload)),
                    created_at,
                ),
            )

    def record_trace_step(
        self,
        session_id: str,
        *,
        step: PlanningStep | ExecutionStep,
    ) -> None:
        created_at = step.completed_at
        action_id = step.action_id if isinstance(step, ExecutionStep) else None
        capability_id = step.capability_id if isinstance(step, ExecutionStep) else None
        capability_version = step.capability_version if isinstance(step, ExecutionStep) else None
        status = step.status.value if isinstance(step, ExecutionStep) else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO trace_steps (
                    session_id,
                    trace_id,
                    step_id,
                    step_type,
                    action_id,
                    capability_id,
                    capability_version,
                    status,
                    record_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    step.trace_id,
                    step.step_id,
                    step.step_type.value,
                    action_id,
                    capability_id,
                    capability_version,
                    status,
                    self._encode_json_blob(step.model_dump(mode="json")),
                    created_at,
                ),
            )

    def record_trace_edge(self, session_id: str, *, edge: TraceEdge) -> None:
        created_at = _utcnow()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_edges (
                    session_id,
                    trace_id,
                    source_step_id,
                    target_step_id,
                    edge_kind,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    edge.trace_id,
                    edge.source_step_id,
                    edge.target_step_id,
                    edge.edge_kind.value,
                    created_at,
                ),
            )

    def record_trace_edges(self, session_id: str, *, edges: list[TraceEdge]) -> None:
        for edge in edges:
            self.record_trace_edge(session_id, edge=edge)

    def list_traces(self, *, limit: int = 20) -> list[TraceSummary]:
        sessions = self.list_sessions(limit=limit)
        with self._connect() as connection:
            traces = [
                self._trace_summary_for_session(
                    connection,
                    session_id=session.session_id,
                    session_row=None,
                )
                for session in sessions
            ]
        return [trace for trace in traces if trace.step_count > 0]

    def get_trace(self, query: TraceQuery) -> TraceRecord | None:
        with self._connect() as connection:
            session_row = self._session_row(connection, query.session_id)
            if session_row is None:
                return None
            all_steps = self._load_trace_steps(connection, session_id=query.session_id)
            all_edges = self._load_trace_edges(connection, session_id=query.session_id)

        trace_summary = self._trace_summary_for_session(
            None,
            session_id=query.session_id,
            session_row=session_row,
            trace_steps=all_steps,
        )
        request_id = next((step.request_id for step in all_steps), None)

        selected_step_ids: set[str] | None = None
        if query.step_id is not None:
            selected_step_ids = self._step_subset(
                query.step_id,
                steps=all_steps,
                edges=all_edges,
                include_predecessors=query.include_predecessors,
            )
            if not selected_step_ids:
                return None

        steps = (
            all_steps
            if selected_step_ids is None
            else [step for step in all_steps if step.step_id in selected_step_ids]
        )
        edges = (
            all_edges
            if selected_step_ids is None
            else [
                edge
                for edge in all_edges
                if edge.source_step_id in selected_step_ids
                and edge.target_step_id in selected_step_ids
            ]
        )

        return TraceRecord(
            trace_id=query.session_id,
            session_id=query.session_id,
            request_id=request_id,
            request_text=session_row["request_text"],
            status=session_row["status"],
            started_at=session_row["started_at"],
            completed_at=session_row["completed_at"],
            steps=steps,
            edges=edges,
            summary=trace_summary,
        )

    def get_audit_report(self, query: TraceQuery) -> AuditReport | None:
        trace = self.get_trace(query)
        if trace is None:
            return None
        missing_fields_by_step = {
            step.step_id: missing
            for step in trace.steps
            if (missing := self._missing_trace_fields(step))
        }
        completeness_passed = not missing_fields_by_step
        notes: list[str] = []
        if query.step_id is not None and query.include_predecessors:
            notes.append("Showing the requested step plus its causal predecessors.")
        elif query.step_id is not None:
            notes.append("Showing the requested step only.")
        elif not trace.steps:
            notes.append("No trace steps were recorded for this session.")
        return AuditReport(
            trace_summary=trace.summary,
            inspected_step_id=query.step_id,
            steps=trace.steps,
            edges=trace.edges,
            completeness_passed=completeness_passed,
            missing_fields_by_step=missing_fields_by_step,
            notes=notes,
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
                    AND assistant_plans.iteration = (
                        SELECT COALESCE(MAX(ap2.iteration), 1)
                        FROM assistant_plans ap2
                        WHERE ap2.session_id = sessions.id
                    )
                WHERE sessions.id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None

            approval_rows = connection.execute(
                """
                SELECT request_json, resolution_json
                FROM capability_approvals
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
            if approval_rows:
                approvals = []
                for row in approval_rows:
                    request_payload = self._load_json_dict(row["request_json"]) or {}
                    resolution_payload = self._load_json_dict(row["resolution_json"]) or {}
                    approvals.append(
                        HistoryApprovalRecord.model_validate(
                            {
                                "action_id": request_payload.get("action_id"),
                                "capability_id": request_payload.get("capability_id"),
                                "mode": resolution_payload.get("mode"),
                                "status": resolution_payload.get("status"),
                                "reason": resolution_payload.get("reason"),
                                "risk_categories": request_payload.get("risk_categories", []),
                                "reason_codes": resolution_payload.get("reason_codes", []),
                                "requested_side_effects": request_payload.get(
                                    "requested_side_effects",
                                    [],
                                ),
                                "command_preview": request_payload.get("command_preview"),
                                "requested_at": resolution_payload.get("requested_at"),
                                "resolved_at": resolution_payload.get("resolved_at"),
                            }
                        )
                    )
            else:
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

            policy_evaluations = [
                PolicyEvaluationRecord.model_validate(
                    self._load_json_dict(row["record_json"]) or {}
                )
                for row in connection.execute(
                    """
                    SELECT record_json
                    FROM policy_evaluations
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
            policy_evaluations=policy_evaluations,
            tool_calls=tool_calls,
            commands=commands,
            events=events,
        )

    def _session_row(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
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
                AND assistant_plans.iteration = (
                    SELECT COALESCE(MAX(ap2.iteration), 1)
                    FROM assistant_plans ap2
                    WHERE ap2.session_id = sessions.id
                )
            WHERE sessions.id = ?
            """,
            (session_id,),
        ).fetchone()

    def _load_trace_steps(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
    ) -> list[PlanningStep | ExecutionStep]:
        rows = connection.execute(
            """
            SELECT record_json
            FROM trace_steps
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            self._trace_step_from_payload(self._load_json_dict(row["record_json"]) or {})
            for row in rows
        ]

    def _load_trace_edges(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
    ) -> list[TraceEdge]:
        rows = connection.execute(
            """
            SELECT trace_id, source_step_id, target_step_id, edge_kind
            FROM trace_edges
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            TraceEdge(
                trace_id=row["trace_id"],
                source_step_id=row["source_step_id"],
                target_step_id=row["target_step_id"],
                edge_kind=row["edge_kind"],
            )
            for row in rows
        ]

    def _trace_step_from_payload(
        self,
        payload: dict[str, object],
    ) -> PlanningStep | ExecutionStep:
        step_type = payload.get("step_type")
        if step_type == "planning":
            return PlanningStep.model_validate(payload)
        return ExecutionStep.model_validate(payload)

    def _trace_summary_for_session(
        self,
        connection: sqlite3.Connection | None,
        *,
        session_id: str,
        session_row: sqlite3.Row | None = None,
        trace_steps: list[PlanningStep | ExecutionStep] | None = None,
    ) -> TraceSummary:
        if connection is None:
            with self._connect() as new_connection:
                return self._trace_summary_for_session(
                    new_connection,
                    session_id=session_id,
                    session_row=session_row,
                    trace_steps=trace_steps,
                )
        row = session_row or self._session_row(connection, session_id)
        if row is None:
            raise KeyError(f"Unknown session id {session_id}.")
        steps = trace_steps or self._load_trace_steps(connection, session_id=session_id)
        execution_steps = [step for step in steps if isinstance(step, ExecutionStep)]
        capability_ids = sorted(
            {
                step.capability_id
                for step in execution_steps
                if step.capability_id is not None
            }
        )
        return TraceSummary(
            trace_id=session_id,
            session_id=session_id,
            request_text=row["request_text"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            step_count=len(steps),
            executed_steps=sum(step.status is ExecutionStatus.EXECUTED for step in execution_steps),
            pending_approval_steps=sum(
                step.status is ExecutionStatus.PENDING_APPROVAL for step in execution_steps
            ),
            blocked_steps=sum(step.status is ExecutionStatus.BLOCKED for step in execution_steps),
            failed_steps=sum(step.status is ExecutionStatus.FAILED for step in execution_steps),
            skipped_steps=sum(
                step.status is ExecutionStatus.NOT_EXECUTED for step in execution_steps
            ),
            selected_capability_ids=capability_ids,
        )

    def _step_subset(
        self,
        step_id: str,
        *,
        steps: list[PlanningStep | ExecutionStep],
        edges: list[TraceEdge],
        include_predecessors: bool,
    ) -> set[str]:
        available_step_ids = {step.step_id for step in steps}
        if step_id not in available_step_ids:
            return set()
        selected = {step_id}
        if not include_predecessors:
            return selected

        predecessors_by_target: dict[str, set[str]] = {}
        for edge in edges:
            predecessors_by_target.setdefault(edge.target_step_id, set()).add(edge.source_step_id)

        stack = [step_id]
        while stack:
            current = stack.pop()
            for predecessor in predecessors_by_target.get(current, set()):
                if predecessor not in selected:
                    selected.add(predecessor)
                    stack.append(predecessor)
        return selected

    def _missing_trace_fields(self, step: PlanningStep | ExecutionStep) -> list[str]:
        missing: list[str] = []
        if not step.request_id:
            missing.append("request_id")
        if not step.started_at:
            missing.append("started_at")
        if not step.completed_at:
            missing.append("completed_at")

        if isinstance(step, PlanningStep):
            if not step.candidate_capability_ids:
                missing.append("candidate_capability_ids")
            if not step.artifacts:
                missing.append("artifacts")
            return missing

        if not step.selection_reason.summary:
            missing.append("selection_reason")
        if step.capability_id is not None and step.capability_version is None:
            missing.append("capability_version")
        if step.capability_id is not None and step.manifest_fingerprint is None:
            missing.append("manifest_fingerprint")
        if step.capability_id is not None and step.policy_evaluation is None:
            missing.append("policy_evaluation")
        if not step.artifacts:
            missing.append("artifacts")
        return missing

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.executescript(_SCHEMA_SQL)
            if current_version < 4:
                self._migrate_to_v4(connection)
            if current_version < 5:
                self._migrate_to_v5(connection)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _migrate_to_v4(connection: sqlite3.Connection) -> None:
        """Migrate from schema v3 to v4: add iteration columns."""
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(assistant_plans)").fetchall()
        }
        if "iteration" not in columns:
            connection.execute(
                "ALTER TABLE assistant_plans ADD COLUMN iteration INTEGER NOT NULL DEFAULT 1"
            )
        so_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(summarized_outcomes)").fetchall()
        }
        if "total_iterations" not in so_columns:
            connection.execute(
                "ALTER TABLE summarized_outcomes "
                "ADD COLUMN total_iterations INTEGER NOT NULL DEFAULT 1"
            )

    @staticmethod
    def _migrate_to_v5(connection: sqlite3.Connection) -> None:
        """Migrate from schema v4 to v5: rename REPLAN edges to REPLANNED_FROM."""
        connection.execute(
            "UPDATE trace_edges SET edge_kind = 'replanned_from' "
            "WHERE edge_kind = 'replan'"
        )

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


class TraceStore(HistoryStore):
    """Stage 3 trace-aware store built on the history database."""
