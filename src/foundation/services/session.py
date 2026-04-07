"""Persistent Stage 00 chat sessions, memory loading, and compaction."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from foundation.models import (
    BrainSession,
    MemoryEnvelope,
    MemoryLayer,
    MemorySource,
    ProviderMessage,
    ProviderMessageRole,
    ResumeTarget,
    SessionCheckpoint,
    SessionSnapshot,
)

logger = logging.getLogger("foundation.services.session")

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS brain_sessions (
    id TEXT PRIMARY KEY,
    workspace_root TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    initial_cwd TEXT NOT NULL,
    current_cwd TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    model TEXT NOT NULL,
    summary_text TEXT NOT NULL DEFAULT '',
    recent_turns_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checkpoint_at TEXT,
    interrupted_turn_json TEXT
);

CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES brain_sessions(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    turn_kind TEXT NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES brain_sessions(id) ON DELETE CASCADE,
    checkpoint_index INTEGER NOT NULL,
    current_cwd TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model TEXT NOT NULL,
    summary_text TEXT NOT NULL DEFAULT '',
    recent_turns_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, checkpoint_index)
);

CREATE INDEX IF NOT EXISTS idx_brain_sessions_updated_at
    ON brain_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_turns_session_id
    ON session_turns(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_session_checkpoints_session_id
    ON session_checkpoints(session_id, checkpoint_index DESC);
"""


def _utcnow() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


class ConversationCompactor:
    """Compact older turns into a durable summary while keeping a recent window."""

    def __init__(
        self,
        *,
        compact_threshold_messages: int = 12,
        max_recent_messages: int = 8,
        preview_characters: int = 240,
        max_summary_characters: int = 6000,
    ) -> None:
        if compact_threshold_messages <= 0:
            raise ValueError("compact_threshold_messages must be positive.")
        if max_recent_messages <= 0:
            raise ValueError("max_recent_messages must be positive.")
        if max_recent_messages > compact_threshold_messages:
            raise ValueError(
                "max_recent_messages must be less than or equal to compact_threshold_messages."
            )
        self._compact_threshold_messages = compact_threshold_messages
        self._max_recent_messages = max_recent_messages
        self._preview_characters = preview_characters
        self._max_summary_characters = max_summary_characters

    def compact(
        self,
        summary_text: str,
        recent_turns: list[ProviderMessage],
        *,
        force: bool = False,
    ) -> tuple[str, list[ProviderMessage], bool]:
        """Return a compacted summary plus the bounded recent turn window."""
        if not recent_turns:
            return summary_text, recent_turns, False
        if not force and len(recent_turns) <= self._compact_threshold_messages:
            return summary_text, recent_turns, False
        if len(recent_turns) <= self._max_recent_messages:
            return summary_text, recent_turns, False

        keep_from = len(recent_turns) - self._max_recent_messages
        compacted_turns = recent_turns[:keep_from]
        kept_turns = list(recent_turns[keep_from:])
        compacted_summary = self._render_summary_block(compacted_turns)
        if not compacted_summary:
            return summary_text, kept_turns, False

        sections: list[str] = []
        if summary_text.strip():
            sections.append(summary_text.strip())
        sections.append(compacted_summary)
        combined = "\n\n".join(sections).strip()
        if len(combined) > self._max_summary_characters:
            truncated = combined[-self._max_summary_characters :].lstrip()
            combined = "[Earlier compacted context truncated]\n" + truncated
        return combined, kept_turns, True

    def _render_summary_block(self, messages: list[ProviderMessage]) -> str:
        lines: list[str] = []
        for message in messages:
            lines.append(
                f"- {message.role.value.title()}: {self._preview_text(message.content)}"
            )
        if not lines:
            return ""
        return "Compacted conversation context:\n" + "\n".join(lines)

    def _preview_text(self, value: str) -> str:
        flattened = " ".join(value.split())
        if len(flattened) <= self._preview_characters:
            return flattened
        return flattened[: self._preview_characters].rstrip() + "..."


class SessionManager:
    """Persist and resume Stage 00 conversational sessions."""

    def __init__(
        self,
        *,
        database_path: Path,
        workspace_root: Path,
        config_dir: Path,
        provider_name: str,
        compactor: ConversationCompactor | None = None,
    ) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._config_dir = Path(config_dir).expanduser().resolve()
        self._provider_name = provider_name.strip().lower()
        self._compactor = compactor or ConversationCompactor()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def database_path(self) -> Path:
        """Return the SQLite file that stores brain sessions."""
        return self._database_path

    @property
    def global_memory_path(self) -> Path:
        """Return the user-editable global memory markdown file."""
        return (self._config_dir / "FOUNDATION.md").expanduser().resolve()

    @property
    def project_memory_path(self) -> Path:
        """Return the workspace-scoped project memory markdown file."""
        return (self._workspace_root / "FOUNDATION.md").expanduser().resolve()

    def resolve_session(
        self,
        target: ResumeTarget,
        *,
        initial_cwd: Path,
        approval_mode: str,
        model: str,
    ) -> BrainSession:
        """Resume the requested session or create a fresh one when needed."""
        if target.mode == "explicit":
            if target.session_id is None:
                raise ValueError("Explicit resume targets require a session id.")
            session = self.get_session(target.session_id)
            if session is None:
                raise ValueError(f"No session found for id {target.session_id}.")
            return session

        latest = self.latest_session()
        if latest is not None:
            return latest
        return self.create_session(
            initial_cwd=initial_cwd,
            approval_mode=approval_mode,
            model=model,
        )

    def create_session(
        self,
        *,
        initial_cwd: Path,
        approval_mode: str,
        model: str,
    ) -> BrainSession:
        """Create and persist one fresh conversational session."""
        session_id = uuid4().hex
        created_at = _utcnow()
        session = BrainSession(
            session_id=session_id,
            workspace_root=str(self._workspace_root),
            initial_cwd=str(Path(initial_cwd).expanduser().resolve()),
            current_cwd=str(Path(initial_cwd).expanduser().resolve()),
            approval_mode=approval_mode,
            provider_name=self._provider_name,
            model=model,
            summary_text="",
            recent_turns=[],
            turn_count=0,
            created_at=created_at,
            updated_at=created_at,
            last_checkpoint_at=created_at,
            interrupted_turn=None,
            recovered_from_interruption=False,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO brain_sessions (
                    id,
                    workspace_root,
                    provider_name,
                    initial_cwd,
                    current_cwd,
                    approval_mode,
                    model,
                    summary_text,
                    recent_turns_json,
                    created_at,
                    updated_at,
                    last_checkpoint_at,
                    interrupted_turn_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.workspace_root,
                    session.provider_name,
                    session.initial_cwd,
                    session.current_cwd,
                    session.approval_mode,
                    session.model,
                    session.summary_text,
                    self._encode_messages(session.recent_turns),
                    session.created_at,
                    session.updated_at,
                    session.last_checkpoint_at,
                    None,
                ),
            )
        self._insert_checkpoint(session)
        return self.get_session(session.session_id) or session

    def latest_session(self) -> BrainSession | None:
        """Return the newest compatible session for the current workspace/provider."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM brain_sessions
                WHERE workspace_root = ? AND provider_name = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (str(self._workspace_root), self._provider_name),
            ).fetchone()
        if row is None:
            return None
        return self._load_session_from_row(row)

    def get_session(self, session_id: str) -> BrainSession | None:
        """Return one compatible session by id."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM brain_sessions
                WHERE id = ? AND workspace_root = ? AND provider_name = ?
                """,
                (session_id, str(self._workspace_root), self._provider_name),
            ).fetchone()
        if row is None:
            return None
        return self._load_session_from_row(row)

    def list_sessions(self, *, limit: int = 20) -> list[SessionSnapshot]:
        """List compatible sessions ordered by most recently updated."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    brain_sessions.*,
                    (
                        SELECT COUNT(*)
                        FROM session_turns
                        WHERE session_turns.session_id = brain_sessions.id
                    ) AS turn_count
                FROM brain_sessions
                WHERE workspace_root = ? AND provider_name = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (str(self._workspace_root), self._provider_name, limit),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def mark_turn_started(
        self,
        session: BrainSession,
        *,
        user_message: str,
        turn_kind: str,
    ) -> None:
        """Mark a turn as in-flight so resume can recover from interruption."""
        payload = {
            "turn_kind": turn_kind,
            "user_message": user_message,
            "started_at": _utcnow(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE brain_sessions
                SET interrupted_turn_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(payload), payload["started_at"], session.session_id),
            )
        session.interrupted_turn = user_message
        session.recovered_from_interruption = False
        session.updated_at = payload["started_at"]

    def record_turn(
        self,
        session: BrainSession,
        *,
        turn_kind: str,
        user_message: str,
        assistant_message: str,
        metadata: dict[str, object] | None = None,
    ) -> BrainSession:
        """Persist one completed turn, compact it if needed, and checkpoint the state."""
        metadata_payload = metadata or {}
        recent_turns = [
            *session.recent_turns,
            ProviderMessage(role=ProviderMessageRole.USER, content=user_message),
            ProviderMessage(role=ProviderMessageRole.ASSISTANT, content=assistant_message),
        ]
        summary_text, bounded_recent_turns, _ = self._compactor.compact(
            session.summary_text,
            recent_turns,
        )
        session.summary_text = summary_text
        session.recent_turns = bounded_recent_turns
        created_at = _utcnow()
        with self._connect() as connection:
            turn_index = self._next_turn_index(connection, session.session_id)
            connection.execute(
                """
                INSERT INTO session_turns (
                    session_id,
                    turn_index,
                    turn_kind,
                    user_message,
                    assistant_message,
                    metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    turn_index,
                    turn_kind,
                    user_message,
                    assistant_message,
                    _json_dumps(metadata_payload),
                    created_at,
                ),
            )
            self._write_checkpoint(connection, session, created_at=created_at)
        session.turn_count += 1
        session.interrupted_turn = None
        session.recovered_from_interruption = False
        session.updated_at = created_at
        session.last_checkpoint_at = created_at
        return session

    def checkpoint(self, session: BrainSession) -> BrainSession:
        """Persist current session state as a clean checkpoint."""
        created_at = _utcnow()
        with self._connect() as connection:
            self._write_checkpoint(connection, session, created_at=created_at)
        session.interrupted_turn = None
        session.recovered_from_interruption = False
        session.updated_at = created_at
        session.last_checkpoint_at = created_at
        return session

    def compact_session(self, session: BrainSession, *, force: bool = True) -> bool:
        """Compact the current session and checkpoint it if anything changed."""
        summary_text, bounded_recent_turns, changed = self._compactor.compact(
            session.summary_text,
            session.recent_turns,
            force=force,
        )
        if not changed:
            return False
        session.summary_text = summary_text
        session.recent_turns = bounded_recent_turns
        self.checkpoint(session)
        return True

    def reset_session(
        self,
        session: BrainSession,
        *,
        current_cwd: Path,
        approval_mode: str,
        model: str,
    ) -> BrainSession:
        """Clear session-local transcript state and reset mutable controls."""
        session.current_cwd = str(Path(current_cwd).expanduser().resolve())
        session.approval_mode = approval_mode
        session.model = model
        session.summary_text = ""
        session.recent_turns = []
        session.interrupted_turn = None
        session.recovered_from_interruption = False
        return self.checkpoint(session)

    def build_memory_envelope(self, session: BrainSession) -> MemoryEnvelope:
        """Load all configured memory layers in deterministic prompt order."""
        layers: list[MemoryLayer] = []
        prompt_messages: list[ProviderMessage] = []
        for source, label, path in (
            (MemorySource.GLOBAL, "Global user memory", self.global_memory_path),
            (MemorySource.PROJECT, "Project memory", self.project_memory_path),
        ):
            content = self._read_memory_file(path)
            layers.append(
                MemoryLayer(
                    source=source,
                    label=label,
                    path=str(path),
                    exists=path.exists(),
                    content=content,
                )
            )
            if content.strip():
                prompt_messages.append(
                    ProviderMessage(
                        role=ProviderMessageRole.DEVELOPER,
                        content=f"{label}:\n{content.strip()}",
                    )
                )

        layers.append(
            MemoryLayer(
                source=MemorySource.SESSION_SUMMARY,
                label="Session summary",
                path=None,
                exists=bool(session.summary_text.strip()),
                content=session.summary_text,
            )
        )
        if session.summary_text.strip():
            prompt_messages.append(
                ProviderMessage(
                    role=ProviderMessageRole.DEVELOPER,
                    content=f"Compacted session summary:\n{session.summary_text.strip()}",
                )
            )

        recent_turns_content = self._render_recent_turns(session.recent_turns)
        layers.append(
            MemoryLayer(
                source=MemorySource.RECENT_TURNS,
                label="Recent turns",
                path=None,
                exists=bool(session.recent_turns),
                content=recent_turns_content,
            )
        )
        prompt_messages.extend(session.recent_turns)
        return MemoryEnvelope(layers=layers, prompt_messages=prompt_messages)

    def write_memory(
        self,
        source: MemorySource,
        *,
        content: str,
        append: bool,
    ) -> MemoryLayer:
        """Write or append to a markdown-backed global or project memory file."""
        if source is MemorySource.SESSION_SUMMARY or source is MemorySource.RECENT_TURNS:
            raise ValueError(f"Memory source {source.value!r} is not file-backed.")
        path = self._memory_path_for_source(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read_memory_file(path)
        if append and existing.strip():
            normalized = existing.rstrip() + "\n\n" + content.strip()
        else:
            normalized = content.strip()
        if normalized:
            normalized += "\n"
        path.write_text(normalized, encoding="utf-8")
        return MemoryLayer(
            source=source,
            label="Global user memory" if source is MemorySource.GLOBAL else "Project memory",
            path=str(path),
            exists=path.exists(),
            content=self._read_memory_file(path),
        )

    def _memory_path_for_source(self, source: MemorySource) -> Path:
        if source is MemorySource.GLOBAL:
            return self.global_memory_path
        if source is MemorySource.PROJECT:
            return self.project_memory_path
        raise ValueError(f"Memory source {source.value!r} is not file-backed.")

    def _insert_checkpoint(self, session: BrainSession) -> None:
        with self._connect() as connection:
            self._write_checkpoint(connection, session, created_at=session.created_at)

    def _write_checkpoint(
        self,
        connection: sqlite3.Connection,
        session: BrainSession,
        *,
        created_at: str,
    ) -> None:
        checkpoint_index = self._next_checkpoint_index(connection, session.session_id)
        connection.execute(
            """
            INSERT INTO session_checkpoints (
                session_id,
                checkpoint_index,
                current_cwd,
                approval_mode,
                provider_name,
                model,
                summary_text,
                recent_turns_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                checkpoint_index,
                session.current_cwd,
                session.approval_mode,
                session.provider_name,
                session.model,
                session.summary_text,
                self._encode_messages(session.recent_turns),
                created_at,
            ),
        )
        connection.execute(
            """
            UPDATE brain_sessions
            SET
                current_cwd = ?,
                approval_mode = ?,
                model = ?,
                summary_text = ?,
                recent_turns_json = ?,
                updated_at = ?,
                last_checkpoint_at = ?,
                interrupted_turn_json = NULL
            WHERE id = ?
            """,
            (
                session.current_cwd,
                session.approval_mode,
                session.model,
                session.summary_text,
                self._encode_messages(session.recent_turns),
                created_at,
                created_at,
                session.session_id,
            ),
        )
        session.latest_checkpoint = SessionCheckpoint(
            session_id=session.session_id,
            checkpoint_index=checkpoint_index,
            current_cwd=session.current_cwd,
            approval_mode=session.approval_mode,
            provider_name=session.provider_name,
            model=session.model,
            summary_text=session.summary_text,
            recent_turns=list(session.recent_turns),
            created_at=created_at,
        )

    def _load_session_from_row(self, row: sqlite3.Row) -> BrainSession:
        interrupted_payload = self._load_json_dict(row["interrupted_turn_json"])
        latest_checkpoint = self._latest_checkpoint(row["id"])
        snapshot = self._snapshot_from_row(row)
        session = BrainSession(
            **snapshot.model_dump(),
            latest_checkpoint=latest_checkpoint,
        )
        if latest_checkpoint is not None:
            session.current_cwd = latest_checkpoint.current_cwd
            session.approval_mode = latest_checkpoint.approval_mode
            session.model = latest_checkpoint.model
            session.summary_text = latest_checkpoint.summary_text
            session.recent_turns = list(latest_checkpoint.recent_turns)
        if interrupted_payload is not None:
            session.interrupted_turn = str(interrupted_payload.get("user_message") or "")
            session.recovered_from_interruption = True
        return session

    def _snapshot_from_row(self, row: sqlite3.Row) -> SessionSnapshot:
        interrupted_payload = self._load_json_dict(row["interrupted_turn_json"])
        turn_count = (
            row["turn_count"]
            if "turn_count" in row.keys()
            else self._turn_count(row["id"])
        )
        return SessionSnapshot(
            session_id=row["id"],
            workspace_root=row["workspace_root"],
            initial_cwd=row["initial_cwd"],
            current_cwd=row["current_cwd"],
            approval_mode=row["approval_mode"],
            provider_name=row["provider_name"],
            model=row["model"],
            summary_text=row["summary_text"],
            recent_turns=self._decode_messages(row["recent_turns_json"]),
            turn_count=int(turn_count),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_checkpoint_at=row["last_checkpoint_at"],
            interrupted_turn=(
                None
                if interrupted_payload is None
                else str(interrupted_payload.get("user_message") or "")
            ),
            recovered_from_interruption=interrupted_payload is not None,
        )

    def _latest_checkpoint(self, session_id: str) -> SessionCheckpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM session_checkpoints
                WHERE session_id = ?
                ORDER BY checkpoint_index DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionCheckpoint(
            session_id=row["session_id"],
            checkpoint_index=row["checkpoint_index"],
            current_cwd=row["current_cwd"],
            approval_mode=row["approval_mode"],
            provider_name=row["provider_name"],
            model=row["model"],
            summary_text=row["summary_text"],
            recent_turns=self._decode_messages(row["recent_turns_json"]),
            created_at=row["created_at"],
        )

    def _turn_count(self, session_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS turn_count FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return int(row["turn_count"])

    def _next_turn_index(self, connection: sqlite3.Connection, session_id: str) -> int:
        row = connection.execute(
            (
                "SELECT COALESCE(MAX(turn_index), 0) AS turn_index "
                "FROM session_turns WHERE session_id = ?"
            ),
            (session_id,),
        ).fetchone()
        assert row is not None
        return int(row["turn_index"]) + 1

    def _next_checkpoint_index(self, connection: sqlite3.Connection, session_id: str) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(checkpoint_index), -1) AS checkpoint_index
            FROM session_checkpoints
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        return int(row["checkpoint_index"]) + 1

    def _encode_messages(self, messages: list[ProviderMessage]) -> str:
        return _json_dumps([message.model_dump(mode="json") for message in messages])

    def _decode_messages(self, raw: str) -> list[ProviderMessage]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("session_message_decode_failed database_path=%s", self._database_path)
            return []
        if not isinstance(payload, list):
            return []
        messages: list[ProviderMessage] = []
        for item in payload:
            try:
                messages.append(ProviderMessage.model_validate(item))
            except Exception:
                logger.warning(
                    "session_message_decode_failed database_path=%s invalid_item=%s",
                    self._database_path,
                    item,
                )
        return messages

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _read_memory_file(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("memory_read_failed path=%s error=%s", path, exc)
            return ""

    def _render_recent_turns(self, recent_turns: list[ProviderMessage]) -> str:
        lines = [
            f"{message.role.value.title()}: {message.content}"
            for message in recent_turns
        ]
        return "\n\n".join(lines)

    def _load_json_dict(self, raw: str | None) -> dict[str, object] | None:
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
        return None
