"""Typed Stage 6 history, approval, and audit models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class SessionKind(StrEnum):
    """Top-level session kinds persisted by the CLI."""

    CHAT = "chat"
    RUN = "run"


class SessionStatus(StrEnum):
    """Terminal session lifecycle states."""

    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalDecisionStatus(StrEnum):
    """Recorded approval outcomes."""

    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


class ApprovalRequest(StrictModel):
    """One approval prompt assembled from policy output."""

    action_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    risk_categories: list[str] = Field(default_factory=list)
    command_preview: str | None = None
    cwd: str | None = None
    paths: list[str] = Field(default_factory=list)


class ApprovalResolution(StrictModel):
    """Normalized approval result used by orchestration and persistence."""

    action_id: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    status: ApprovalDecisionStatus
    reason: str = Field(min_length=1)
    requested_at: str = Field(min_length=1)
    resolved_at: str = Field(min_length=1)
    risk_categories: list[str] = Field(default_factory=list)
    command_preview: str | None = None


class HistoryEventRecord(StrictModel):
    """One audit-log event attached to a session."""

    event_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(min_length=1)


class HistoryApprovalRecord(StrictModel):
    """A persisted approval request and outcome."""

    action_id: str | None = None
    mode: str = Field(min_length=1)
    status: ApprovalDecisionStatus
    reason: str = Field(min_length=1)
    risk_categories: list[str] = Field(default_factory=list)
    command_preview: str | None = None
    requested_at: str = Field(min_length=1)
    resolved_at: str = Field(min_length=1)


class HistoryToolCallRecord(StrictModel):
    """A persisted tool invocation proposed by the model."""

    action_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_decision: str | None = None
    policy_reason: str | None = None
    risk_categories: list[str] = Field(default_factory=list)
    execution_status: str = Field(min_length=1)
    artifact: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = Field(min_length=1)


class HistoryCommandRecord(StrictModel):
    """A persisted shell command execution or pending shell action."""

    action_id: str | None = None
    source: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    mode: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    risk_categories: list[str] = Field(default_factory=list)
    execution_status: str = Field(min_length=1)
    exit_code: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None
    created_at: str = Field(min_length=1)


class HistorySessionSummary(StrictModel):
    """Compact list view for persisted sessions."""

    session_id: str = Field(min_length=1)
    kind: SessionKind
    status: SessionStatus
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    workspace_root: str = Field(min_length=1)
    request_cwd: str = Field(min_length=1)
    approval_mode: str = Field(min_length=1)
    plan_only: bool = False
    request_text: str | None = None
    command_preview: str | None = None
    summary_text: str | None = None
    executed_actions: int = Field(default=0, ge=0)
    pending_approval_actions: int = Field(default=0, ge=0)
    blocked_actions: int = Field(default=0, ge=0)
    failed_actions: int = Field(default=0, ge=0)
    skipped_actions: int = Field(default=0, ge=0)


class HistorySessionDetail(HistorySessionSummary):
    """Expanded detail view for one session."""

    assistant_message: str | None = None
    context: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    planning_metadata: dict[str, Any] | None = None
    approvals: list[HistoryApprovalRecord] = Field(default_factory=list)
    tool_calls: list[HistoryToolCallRecord] = Field(default_factory=list)
    commands: list[HistoryCommandRecord] = Field(default_factory=list)
    events: list[HistoryEventRecord] = Field(default_factory=list)


class StagedWorkspaceWrite(StrictModel):
    """A temp-file rewrite staged for a later atomic replacement."""

    target_path: str = Field(min_length=1)
    staged_path: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
