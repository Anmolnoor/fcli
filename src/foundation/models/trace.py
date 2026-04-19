"""Typed Stage 3 trace, audit, and step models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundation.models.capability import PolicyEvaluationRecord
from foundation.models.history import ApprovalRequest, ApprovalResolution, SessionStatus
from foundation.models.orchestration import ExecutionStatus, ProviderResponseMetadata


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class TraceStepType(StrEnum):
    """Supported persisted step kinds."""

    PLANNING = "planning"
    EXECUTION = "execution"


class TraceEdgeKind(StrEnum):
    """Causal edge kinds between persisted steps."""

    PLANNED = "planned"
    DEPENDS_ON = "depends_on"
    SEQUENTIAL = "sequential"
    REPLANNED_FROM = "replanned_from"


class TraceArtifactRole(StrEnum):
    """Artifact roles persisted for audit and later replay work."""

    INPUT = "input"
    OUTPUT = "output"
    CONTEXT = "context"
    POLICY = "policy"
    APPROVAL = "approval"


class SideEffectStatus(StrEnum):
    """Observed status for one recorded side effect."""

    DECLARED = "declared"
    OBSERVED = "observed"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"


class SelectionReason(StrictModel):
    """Why one capability, or no capability, was selected."""

    selected_capability_id: str | None = None
    candidate_capability_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    detail: str | None = None


class TraceArtifactRef(StrictModel):
    """Reference to one input, output, or policy artifact."""

    name: str = Field(min_length=1)
    role: TraceArtifactRole
    storage_ref: str = Field(min_length=1)
    mime_type: str | None = None
    sha256: str | None = Field(default=None, min_length=32)
    size_bytes: int | None = Field(default=None, ge=0)
    truncated: bool = False
    preview: str | None = None


class StepSideEffect(StrictModel):
    """One declared or observed side effect attached to a step."""

    kind: str = Field(min_length=1)
    status: SideEffectStatus
    target: str | None = None
    summary: str = Field(min_length=1)


class PlanningStep(StrictModel):
    """One persisted planning step for a request."""

    step_type: Literal[TraceStepType.PLANNING] = TraceStepType.PLANNING
    step_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_text: str = Field(min_length=1)
    request_cwd: str = Field(min_length=1)
    iteration_index: int = Field(default=1, ge=1)
    candidate_capability_ids: list[str] = Field(default_factory=list)
    selection_reasons: list[SelectionReason] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    planning_metadata: ProviderResponseMetadata
    artifacts: list[TraceArtifactRef] = Field(default_factory=list)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)


class ExecutionStep(StrictModel):
    """One persisted execution or non-execution step for a planned action."""

    step_type: Literal[TraceStepType.EXECUTION] = TraceStepType.EXECUTION
    step_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    action_summary: str = Field(min_length=1)
    status: ExecutionStatus
    request_cwd: str = Field(min_length=1)
    iteration_index: int = Field(default=1, ge=1)
    capability_id: str | None = None
    capability_version: str | None = None
    capability_name: str | None = None
    manifest_fingerprint: str | None = Field(default=None, min_length=32)
    policy_snapshot_version: str | None = None
    selection_reason: SelectionReason
    policy_evaluation: PolicyEvaluationRecord | None = None
    approval_request: ApprovalRequest | None = None
    approval_resolution: ApprovalResolution | None = None
    artifacts: list[TraceArtifactRef] = Field(default_factory=list)
    side_effects: list[StepSideEffect] = Field(default_factory=list)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0.0)
    error: str | None = None


TraceStep = Annotated[PlanningStep | ExecutionStep, Field(discriminator="step_type")]


class TraceEdge(StrictModel):
    """One causal link between two stored steps."""

    trace_id: str = Field(min_length=1)
    source_step_id: str = Field(min_length=1)
    target_step_id: str = Field(min_length=1)
    edge_kind: TraceEdgeKind


class TraceSummary(StrictModel):
    """Compact list view for one trace or session."""

    trace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_text: str | None = None
    status: SessionStatus
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    step_count: int = Field(default=0, ge=0)
    executed_steps: int = Field(default=0, ge=0)
    pending_approval_steps: int = Field(default=0, ge=0)
    blocked_steps: int = Field(default=0, ge=0)
    failed_steps: int = Field(default=0, ge=0)
    skipped_steps: int = Field(default=0, ge=0)
    selected_capability_ids: list[str] = Field(default_factory=list)


class TraceRecord(StrictModel):
    """Full reconstructed trace for one request."""

    trace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_id: str | None = None
    request_text: str | None = None
    status: SessionStatus
    started_at: str = Field(min_length=1)
    completed_at: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    edges: list[TraceEdge] = Field(default_factory=list)
    summary: TraceSummary


class TraceQuery(StrictModel):
    """Query parameters for trace and audit inspection."""

    session_id: str = Field(min_length=1)
    step_id: str | None = None
    include_predecessors: bool = False
    limit: int = Field(default=20, ge=1)


class AuditReport(StrictModel):
    """Audit-first explanation of one stored trace or step."""

    trace_summary: TraceSummary
    inspected_step_id: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    edges: list[TraceEdge] = Field(default_factory=list)
    completeness_passed: bool = True
    missing_fields_by_step: dict[str, list[str]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
