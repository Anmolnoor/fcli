"""Typed presentation models for quiet chat output."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class RenderMode(StrEnum):
    """Presentation modes for chat output."""

    CONCISE = "concise"
    VERBOSE = "verbose"


class PresentationNoticeLevel(StrEnum):
    """Severity levels for concise chat notices."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    DIM = "dim"


class TerminalLogRouting(StrEnum):
    """Where runtime logger output should be routed."""

    FILE_ONLY = "file_only"
    FILE_AND_TERMINAL = "file_and_terminal"
    TERMINAL_ONLY = "terminal_only"


class InteractiveDetailCommand(StrEnum):
    """Detail inspection commands available in interactive chat."""

    PLAN = "/plan"
    ACTIONS = "/actions"
    SUMMARY = "/summary"


class AuditDetailRef(StrictModel):
    """Pointers to persisted detail that stays hidden in concise mode."""

    session_id: str = Field(min_length=1)
    history_hint: str = Field(min_length=1)
    trace_hint: str = Field(min_length=1)


class ChatNotice(StrictModel):
    """One user-visible notice attached to a concise chat turn."""

    level: PresentationNoticeLevel = PresentationNoticeLevel.INFO
    text: str = Field(min_length=1)


class ChatSurfacePolicy(StrictModel):
    """Rules that decide how a chat turn is rendered."""

    render_mode: RenderMode = RenderMode.CONCISE
    show_audit_refs: bool = True


class ChatTurnPresentation(StrictModel):
    """Primary chat output plus optional concise notices."""

    primary_text: str = Field(min_length=1)
    notices: list[ChatNotice] = Field(default_factory=list)
    audit_ref: AuditDetailRef | None = None
