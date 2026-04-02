"""Stage 6 approval request and resolution helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from foundation.models import PlannedAction, PolicyDecision, PolicyDecisionType
from foundation.models.history import (
    ApprovalDecisionStatus,
    ApprovalRequest,
    ApprovalResolution,
)
from foundation.settings import ApprovalMode

ApprovalPrompt = Callable[[ApprovalRequest], bool]


def _utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ApprovalService:
    """Resolve approval-required policy outcomes according to the configured mode."""

    def __init__(
        self,
        *,
        mode: ApprovalMode,
        prompt_callback: ApprovalPrompt | None = None,
    ) -> None:
        self._mode = mode
        self._prompt_callback = prompt_callback

    def build_request(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        *,
        request_cwd: Path,
    ) -> ApprovalRequest:
        if decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL:
            raise ValueError("Approval requests can only be built for approval-required actions.")
        return ApprovalRequest(
            action_id=action.id,
            summary=action.summary,
            reason=decision.reason,
            risk_categories=list(decision.risk_categories),
            command_preview=decision.command_preview,
            cwd=str(request_cwd),
            paths=list(decision.paths),
        )

    def resolve(
        self,
        action: PlannedAction,
        decision: PolicyDecision,
        *,
        request_cwd: Path,
    ) -> tuple[ApprovalRequest, ApprovalResolution]:
        request = self.build_request(action, decision, request_cwd=request_cwd)
        requested_at = _utcnow()
        resolved_at = requested_at

        if self._mode is ApprovalMode.AUTO:
            return request, ApprovalResolution(
                action_id=action.id,
                mode=self._mode.value,
                status=ApprovalDecisionStatus.APPROVED,
                reason="Automatically approved by approval.mode=auto.",
                requested_at=requested_at,
                resolved_at=resolved_at,
                risk_categories=list(request.risk_categories),
                command_preview=request.command_preview,
            )

        if self._mode is ApprovalMode.MANUAL or self._prompt_callback is None:
            return request, ApprovalResolution(
                action_id=action.id,
                mode=self._mode.value,
                status=ApprovalDecisionStatus.PENDING,
                reason="Approval is required before this action can execute.",
                requested_at=requested_at,
                resolved_at=resolved_at,
                risk_categories=list(request.risk_categories),
                command_preview=request.command_preview,
            )

        approved = bool(self._prompt_callback(request))
        return request, ApprovalResolution(
            action_id=action.id,
            mode=self._mode.value,
            status=(
                ApprovalDecisionStatus.APPROVED if approved else ApprovalDecisionStatus.DENIED
            ),
            reason=(
                "Approved by the user at the prompt."
                if approved
                else "Denied by the user at the prompt."
            ),
            requested_at=requested_at,
            resolved_at=_utcnow(),
            risk_categories=list(request.risk_categories),
            command_preview=request.command_preview,
        )
