"""Persistent Stage 00 session and memory models."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foundation.models.orchestration import ProviderMessage


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class MemorySource(StrEnum):
    """Inspectable memory layers loaded into a chat session."""

    GLOBAL = "global"
    PROJECT = "project"
    SESSION_SUMMARY = "session_summary"
    RECENT_TURNS = "recent_turns"


class InteractiveCommand(StrEnum):
    """Slash commands supported by the interactive chat shell."""

    ACTIONS = "/actions"
    APPROVAL = "/approval"
    CLEAR = "/clear"
    COMPACT = "/compact"
    CONFIG = "/config"
    CWD = "/cwd"
    EXIT = "/exit"
    HELP = "/help"
    HISTORY = "/history"
    MEMORY = "/memory"
    MODEL = "/model"
    PLAN = "/plan"
    QUIT = "/quit"
    RESET = "/reset"
    RESUME = "/resume"
    SESSIONS = "/sessions"
    SUMMARY = "/summary"
    TOOLS = "/tools"


class ResumeTarget(StrictModel):
    """Selector for resuming the latest or one explicit session."""

    mode: Literal["latest", "explicit"] = "latest"
    session_id: str | None = None

    @classmethod
    def latest(cls) -> ResumeTarget:
        """Return a selector for the newest compatible session."""
        return cls(mode="latest")

    @classmethod
    def explicit(cls, session_id: str) -> ResumeTarget:
        """Return a selector for one explicit session id."""
        return cls(mode="explicit", session_id=session_id)


class MemoryLayer(StrictModel):
    """One inspectable memory source loaded for a session turn."""

    source: MemorySource
    label: str = Field(min_length=1)
    path: str | None = None
    exists: bool = False
    content: str = ""


class MemoryEnvelope(StrictModel):
    """Loaded memory layers plus provider-ready prompt messages."""

    layers: list[MemoryLayer] = Field(default_factory=list)
    prompt_messages: list[ProviderMessage] = Field(default_factory=list)


class SessionCheckpoint(StrictModel):
    """Durable session checkpoint used for resume and crash recovery."""

    session_id: str = Field(min_length=1)
    checkpoint_index: int = Field(ge=0)
    current_cwd: str = Field(min_length=1)
    approval_mode: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    summary_text: str = ""
    recent_turns: list[ProviderMessage] = Field(default_factory=list)
    created_at: str = Field(min_length=1)


class SessionSnapshot(StrictModel):
    """List/detail view of one persisted chat session."""

    session_id: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    initial_cwd: str = Field(min_length=1)
    current_cwd: str = Field(min_length=1)
    approval_mode: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    summary_text: str = ""
    recent_turns: list[ProviderMessage] = Field(default_factory=list)
    turn_count: int = Field(default=0, ge=0)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    last_checkpoint_at: str | None = None
    interrupted_turn: str | None = None
    recovered_from_interruption: bool = False


class BrainSession(SessionSnapshot):
    """Active conversational session state used by the terminal shell."""

    latest_checkpoint: SessionCheckpoint | None = None
