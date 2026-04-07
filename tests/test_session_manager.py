from __future__ import annotations

from pathlib import Path

from foundation.models import MemorySource, ResumeTarget
from foundation.services import ConversationCompactor, SessionManager


def _session_manager(
    tmp_path: Path,
    *,
    compactor: ConversationCompactor | None = None,
) -> SessionManager:
    workspace_root = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    workspace_root.mkdir()
    config_dir.mkdir()
    state_dir.mkdir()
    return SessionManager(
        database_path=state_dir / "chat-sessions.sqlite3",
        workspace_root=workspace_root,
        config_dir=config_dir,
        provider_name="openai",
        compactor=compactor,
    )


def test_session_manager_loads_memory_layers_in_order(tmp_path: Path) -> None:
    manager = _session_manager(tmp_path)
    session = manager.create_session(
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="gpt-5-mini",
    )
    manager.write_memory(MemorySource.GLOBAL, content="Remember user preferences.", append=False)
    manager.write_memory(MemorySource.PROJECT, content="Project notes live here.", append=False)
    session.summary_text = "Compacted task summary."
    manager.checkpoint(session)
    manager.record_turn(
        session,
        turn_kind="chat",
        user_message="What changed?",
        assistant_message="I checked the repo status.",
    )

    envelope = manager.build_memory_envelope(session)

    assert [layer.source for layer in envelope.layers] == [
        MemorySource.GLOBAL,
        MemorySource.PROJECT,
        MemorySource.SESSION_SUMMARY,
        MemorySource.RECENT_TURNS,
    ]
    assert "Global user memory" in envelope.prompt_messages[0].content
    assert "Project memory" in envelope.prompt_messages[1].content
    assert "Compacted session summary" in envelope.prompt_messages[2].content
    assert envelope.prompt_messages[3].content == "What changed?"
    assert envelope.prompt_messages[4].content == "I checked the repo status."


def test_session_manager_resolves_latest_and_explicit_sessions(tmp_path: Path) -> None:
    manager = _session_manager(tmp_path)
    first = manager.create_session(
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="gpt-5-mini",
    )
    second = manager.create_session(
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="gpt-5",
    )

    latest = manager.resolve_session(
        ResumeTarget.latest(),
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="ignored",
    )
    explicit = manager.resolve_session(
        ResumeTarget.explicit(first.session_id),
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="ignored",
    )

    assert latest.session_id == second.session_id
    assert explicit.session_id == first.session_id


def test_session_manager_recovers_last_checkpoint_after_interrupted_turn(
    tmp_path: Path,
) -> None:
    manager = _session_manager(tmp_path)
    session = manager.create_session(
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="gpt-5-mini",
    )
    manager.record_turn(
        session,
        turn_kind="chat",
        user_message="Inspect the workspace.",
        assistant_message="I looked at the workspace files.",
    )
    manager.mark_turn_started(
        session,
        user_message="Continue from before.",
        turn_kind="chat",
    )

    resumed = manager.get_session(session.session_id)

    assert resumed is not None
    assert resumed.recovered_from_interruption is True
    assert resumed.interrupted_turn == "Continue from before."
    assert [message.content for message in resumed.recent_turns] == [
        "Inspect the workspace.",
        "I looked at the workspace files.",
    ]


def test_session_manager_compacts_older_turns(tmp_path: Path) -> None:
    manager = _session_manager(
        tmp_path,
        compactor=ConversationCompactor(
            compact_threshold_messages=4,
            max_recent_messages=2,
            preview_characters=80,
        ),
    )
    session = manager.create_session(
        initial_cwd=tmp_path / "workspace",
        approval_mode="prompt",
        model="gpt-5-mini",
    )

    manager.record_turn(
        session,
        turn_kind="chat",
        user_message="First request",
        assistant_message="First reply",
    )
    manager.record_turn(
        session,
        turn_kind="chat",
        user_message="Second request",
        assistant_message="Second reply",
    )
    manager.record_turn(
        session,
        turn_kind="chat",
        user_message="Third request",
        assistant_message="Third reply",
    )

    assert "First request" in session.summary_text
    assert len(session.recent_turns) == 2
    assert [message.content for message in session.recent_turns] == [
        "Third request",
        "Third reply",
    ]
