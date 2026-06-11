"""Observer service for Stage 3 event emission, redaction, and trace persistence."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from foundation.models import (
    ActionKind,
    ApprovalRequest,
    ApprovalResolution,
    ContextSnapshot,
    ExecutionResult,
    ExecutionStatus,
    PlannedAction,
    PolicyEvaluationRecord,
    ProviderResponseMetadata,
    SelectionReason,
    SideEffectStatus,
    StepSideEffect,
    TraceArtifactRef,
    TraceArtifactRole,
    TraceEdge,
    TraceEdgeKind,
)
from foundation.models.trace import ExecutionStep, PlanningStep
from foundation.observability import (
    SINK_DISABLE_AFTER_CONSECUTIVE_FAILURES,
    emit_event,
    emit_exception,
    redact_payload,
)
from foundation.services.capabilities import SHELL_CAPABILITY_ID, CapabilityRegistry
from foundation.services.guardrails import POLICY_SNAPSHOT_VERSION
from foundation.services.history import HistoryStore

logger = logging.getLogger("foundation.services.observer")

EventSink = Callable[[str, Mapping[str, Any]], None]


class ObserverService:
    """Emit redacted events and persist Stage 3 trace records."""

    def __init__(
        self,
        *,
        history_store: HistoryStore | None,
        capability_registry: CapabilityRegistry,
        event_sink: EventSink | None = None,
    ) -> None:
        self._history_store = history_store
        self._capability_registry = capability_registry
        self._event_sink: EventSink | None = event_sink
        self._sink_failure_count = 0
        self._sink_consecutive_failures = 0
        self._sink_disabled = False

    def set_event_sink(self, event_sink: EventSink | None) -> None:
        """Replace the event sink callback (or clear it with ``None``)."""
        self._event_sink = event_sink
        self._sink_consecutive_failures = 0
        self._sink_disabled = False

    @property
    def sink_failure_count(self) -> int:
        """Total sink failures suppressed so far (for surfacing degradation)."""
        return self._sink_failure_count

    @property
    def sink_disabled(self) -> bool:
        """Whether the sink was disabled after repeated consecutive failures."""
        return self._sink_disabled

    def _dispatch_to_sink(self, event_name: str, payload: Mapping[str, Any]) -> None:
        if self._event_sink is None or self._sink_disabled:
            return
        try:
            self._event_sink(event_name, payload)
        except Exception:
            # A sink failure must never break the turn, but it must not be
            # silent either: events are the audit surface.
            self._sink_failure_count += 1
            self._sink_consecutive_failures += 1
            if self._sink_consecutive_failures >= SINK_DISABLE_AFTER_CONSECUTIVE_FAILURES:
                self._sink_disabled = True
                logger.warning(
                    "event sink disabled after %d consecutive failures; further "
                    "events will not reach monitor surfaces (event=%s)",
                    self._sink_consecutive_failures,
                    event_name,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "event sink failed; monitor surfaces may be missing events (event=%s)",
                    event_name,
                    exc_info=True,
                )
        else:
            self._sink_consecutive_failures = 0

    def emit(
        self,
        event_name: str,
        *,
        payload: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        logger_name: str = "foundation.services.observer",
        level: int | str = logging.INFO,
    ) -> None:
        emit_event(
            event_name,
            payload=payload,
            logger_name=logger_name,
            level=level,
        )
        if self._history_store is not None and session_id is not None:
            self._history_store.record_event(session_id, event_name, dict(payload or {}))
        if self._event_sink is not None:
            redacted = redact_payload(payload)
            if session_id is not None and "session_id" not in redacted:
                redacted["session_id"] = session_id
            self._dispatch_to_sink(event_name, redacted)

    def emit_exception(
        self,
        event_name: str,
        exc: BaseException,
        *,
        payload: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        logger_name: str = "foundation.services.observer",
        level: int | str = logging.ERROR,
    ) -> None:
        emit_exception(
            event_name,
            exc,
            payload=payload,
            logger_name=logger_name,
            level=level,
        )
        details: dict[str, object] = {
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
        if payload:
            details.update(payload)
        if self._history_store is not None and session_id is not None:
            self._history_store.record_event(session_id, event_name, details)
        if self._event_sink is not None:
            redacted = redact_payload(details)
            if session_id is not None and "session_id" not in redacted:
                redacted["session_id"] = session_id
            self._dispatch_to_sink(event_name, redacted)

    def record_planning_step(
        self,
        session_id: str | None,
        *,
        request_id: str,
        request_text: str,
        context: ContextSnapshot,
        plan_assistant_message: str,
        actions: list[PlannedAction],
        action_ids: list[str],
        planning_metadata: ProviderResponseMetadata,
        started_at: str,
        completed_at: str,
        duration_seconds: float,
        iteration: int = 1,
    ) -> str:
        step_id = self.planning_step_id(request_id, iteration=iteration)
        if self._history_store is None or session_id is None:
            return step_id
        candidate_capability_ids = [
            str(snapshot.capability_id) for snapshot in context.available_capabilities
        ]
        step = PlanningStep(
            step_id=step_id,
            trace_id=session_id,
            session_id=session_id,
            request_id=request_id,
            request_text=request_text,
            request_cwd=context.request_cwd,
            iteration_index=iteration,
            candidate_capability_ids=candidate_capability_ids,
            selection_reasons=[
                self.selection_reason_for_action(
                    action,
                    candidate_capability_ids=candidate_capability_ids,
                )
                for action in actions
            ],
            action_ids=action_ids,
            planning_metadata=planning_metadata,
            artifacts=[
                self._artifact_ref(
                    name="planning_context",
                    role=TraceArtifactRole.CONTEXT,
                    storage_ref=f"history://session/{session_id}/planning/context",
                    payload=context.model_dump(mode="json"),
                ),
                self._artifact_ref(
                    name="assistant_plan",
                    role=TraceArtifactRole.OUTPUT,
                    storage_ref=f"history://session/{session_id}/planning/plan",
                    payload={
                        "assistant_message": plan_assistant_message,
                        "action_ids": action_ids,
                    },
                ),
            ],
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
        )
        self._history_store.record_trace_step(session_id, step=step)
        return step_id

    def record_execution_step(
        self,
        session_id: str | None,
        *,
        request_id: str,
        action: PlannedAction,
        request_cwd: Path,
        execution_result: ExecutionResult,
        policy_evaluation: PolicyEvaluationRecord | None,
        approval_request: ApprovalRequest | None,
        approval_resolution: ApprovalResolution | None,
        candidate_capability_ids: list[str],
        planning_step_id: str,
        prior_step_id: str | None,
        started_at: str,
        completed_at: str,
        duration_seconds: float,
        iteration: int = 1,
    ) -> str:
        step_id = self.execution_step_id(request_id, iteration=iteration, action_id=action.id)
        if self._history_store is None or session_id is None:
            return step_id
        capability_id, capability_version, capability_name, manifest_fingerprint = (
            self._capability_metadata(action)
        )
        selection_reason = self.selection_reason_for_action(
            action,
            candidate_capability_ids=candidate_capability_ids,
        )
        step = ExecutionStep(
            step_id=step_id,
            trace_id=session_id,
            session_id=session_id,
            request_id=request_id,
            action_id=action.id,
            action_summary=action.summary,
            status=execution_result.status,
            request_cwd=str(request_cwd),
            iteration_index=iteration,
            capability_id=capability_id,
            capability_version=capability_version,
            capability_name=capability_name,
            manifest_fingerprint=manifest_fingerprint,
            policy_snapshot_version=(
                POLICY_SNAPSHOT_VERSION if policy_evaluation is not None else None
            ),
            selection_reason=selection_reason,
            policy_evaluation=policy_evaluation,
            approval_request=approval_request,
            approval_resolution=approval_resolution,
            artifacts=self._execution_artifacts(
                session_id=session_id,
                action=action,
                execution_result=execution_result,
                policy_evaluation=policy_evaluation,
                approval_request=approval_request,
                approval_resolution=approval_resolution,
            ),
            side_effects=self._side_effects(
                execution_result=execution_result,
                policy_evaluation=policy_evaluation,
            ),
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            error=execution_result.error,
        )
        self._history_store.record_trace_step(session_id, step=step)
        edges = [
            TraceEdge(
                trace_id=session_id,
                source_step_id=planning_step_id,
                target_step_id=step_id,
                edge_kind=TraceEdgeKind.PLANNED,
            )
        ]
        if prior_step_id is not None:
            edges.append(
                TraceEdge(
                    trace_id=session_id,
                    source_step_id=prior_step_id,
                    target_step_id=step_id,
                    edge_kind=TraceEdgeKind.SEQUENTIAL,
                )
            )
        self._history_store.record_trace_edges(session_id, edges=edges)
        return step_id

    @staticmethod
    def planning_step_id(request_id: str, *, iteration: int = 1) -> str:
        """Return the canonical planning step id for one request iteration."""
        return f"planning:{request_id}:{iteration}"

    @staticmethod
    def execution_step_id(request_id: str, *, iteration: int, action_id: str) -> str:
        """Return the canonical execution step id for one planned action."""
        return f"action:{request_id}:{iteration}:{action_id}"

    def selection_reason_for_action(
        self,
        action: PlannedAction,
        *,
        candidate_capability_ids: list[str],
    ) -> SelectionReason:
        if action.kind is ActionKind.TOOL_CALL:
            assert action.tool_call is not None
            detail = (
                f"Planner selected capability {action.tool_call.capability_id} "
                f"for action {action.id}."
            )
            if action.requires_approval and action.approval_reason:
                detail += f" Approval hint: {action.approval_reason}"
            return SelectionReason(
                selected_capability_id=action.tool_call.capability_id,
                candidate_capability_ids=candidate_capability_ids,
                summary=action.summary,
                detail=detail,
            )
        if action.kind is ActionKind.SHELL:
            detail = f"Planner selected the shell runtime for action {action.id}."
            if action.requires_approval and action.approval_reason:
                detail += f" Approval hint: {action.approval_reason}"
            return SelectionReason(
                selected_capability_id=SHELL_CAPABILITY_ID,
                candidate_capability_ids=candidate_capability_ids,
                summary=action.summary,
                detail=detail,
            )
        return SelectionReason(
            selected_capability_id=None,
            candidate_capability_ids=candidate_capability_ids,
            summary=action.summary,
            detail="Planner answered directly without invoking a capability.",
        )

    def _capability_metadata(
        self,
        action: PlannedAction,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        capability_id: str | None
        version: str | None
        if action.kind is ActionKind.TOOL_CALL:
            assert action.tool_call is not None
            capability_id = action.tool_call.capability_id
            version = action.tool_call.version
        elif action.kind is ActionKind.SHELL:
            capability_id = SHELL_CAPABILITY_ID
            version = None
        else:
            return None, None, None, None

        manifest = self._capability_registry.resolve(capability_id, version)
        manifest_payload = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
        return (
            manifest.id,
            str(manifest.version),
            manifest.name,
            hashlib.sha256(manifest_payload).hexdigest(),
        )

    def _execution_artifacts(
        self,
        *,
        session_id: str,
        action: PlannedAction,
        execution_result: ExecutionResult,
        policy_evaluation: PolicyEvaluationRecord | None,
        approval_request: ApprovalRequest | None,
        approval_resolution: ApprovalResolution | None,
    ) -> list[TraceArtifactRef]:
        artifacts = [
            self._artifact_ref(
                name="planned_action",
                role=TraceArtifactRole.INPUT,
                storage_ref=f"history://session/{session_id}/actions/{action.id}/input",
                payload=action.model_dump(mode="json"),
            )
        ]
        if execution_result.artifact is not None:
            artifacts.append(
                self._artifact_ref(
                    name="execution_result",
                    role=TraceArtifactRole.OUTPUT,
                    storage_ref=f"history://session/{session_id}/actions/{action.id}/output",
                    payload=execution_result.artifact,
                )
            )
        else:
            artifacts.append(
                self._artifact_ref(
                    name="execution_summary",
                    role=TraceArtifactRole.OUTPUT,
                    storage_ref=f"history://session/{session_id}/actions/{action.id}/summary",
                    payload={"summary": execution_result.summary, "error": execution_result.error},
                )
            )
        if policy_evaluation is not None:
            artifacts.append(
                self._artifact_ref(
                    name="policy_evaluation",
                    role=TraceArtifactRole.POLICY,
                    storage_ref=f"history://session/{session_id}/actions/{action.id}/policy",
                    payload=policy_evaluation.model_dump(mode="json"),
                )
            )
        if approval_request is not None:
            artifacts.append(
                self._artifact_ref(
                    name="approval_request",
                    role=TraceArtifactRole.APPROVAL,
                    storage_ref=f"history://session/{session_id}/actions/{action.id}/approval_request",
                    payload=approval_request.model_dump(mode="json"),
                )
            )
        if approval_resolution is not None:
            artifacts.append(
                self._artifact_ref(
                    name="approval_resolution",
                    role=TraceArtifactRole.APPROVAL,
                    storage_ref=f"history://session/{session_id}/actions/{action.id}/approval_resolution",
                    payload=approval_resolution.model_dump(mode="json"),
                )
            )
        return artifacts

    def _side_effects(
        self,
        *,
        execution_result: ExecutionResult,
        policy_evaluation: PolicyEvaluationRecord | None,
    ) -> list[StepSideEffect]:
        if policy_evaluation is None:
            return []
        if execution_result.status is ExecutionStatus.EXECUTED:
            status = SideEffectStatus.OBSERVED
        elif execution_result.status is ExecutionStatus.PENDING_APPROVAL:
            status = SideEffectStatus.PENDING_APPROVAL
        elif execution_result.status is ExecutionStatus.BLOCKED:
            status = SideEffectStatus.BLOCKED
        else:
            status = SideEffectStatus.DECLARED
        return [
            StepSideEffect(
                kind=side_effect,
                status=status,
                summary=f"Requested side effect {side_effect}.",
            )
            for side_effect in policy_evaluation.policy_input.requested_side_effects
        ]

    def _artifact_ref(
        self,
        *,
        name: str,
        role: TraceArtifactRole,
        storage_ref: str,
        payload: object,
    ) -> TraceArtifactRef:
        if isinstance(payload, str):
            preview = payload
            mime_type = "text/plain"
        else:
            preview = json.dumps(payload, ensure_ascii=True, sort_keys=True)
            mime_type = "application/json"
        encoded = preview.encode("utf-8")
        truncated = len(encoded) > 1024
        if truncated:
            preview = encoded[:1024].decode("utf-8", errors="ignore")
        return TraceArtifactRef(
            name=name,
            role=role,
            storage_ref=storage_ref,
            mime_type=mime_type,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            truncated=truncated,
            preview=preview,
        )
