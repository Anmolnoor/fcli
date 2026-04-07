"""Stage 6 approval request and resolution helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from foundation.models import (
    CapabilityApprovalRequest,
    CapabilityApprovalResolution,
    PlannedAction,
    PolicyEvaluationRecord,
)
from foundation.models.history import (
    ApprovalDecisionStatus,
    ApprovalRequest,
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
        evaluation: PolicyEvaluationRecord,
        *,
        request_cwd: Path,
    ) -> CapabilityApprovalRequest:
        if evaluation.verdict.outcome.value != "require_approval":
            raise ValueError("Approval requests can only be built for approval-required actions.")
        policy_input = evaluation.policy_input
        return CapabilityApprovalRequest(
            action_id=action.id,
            capability_id=evaluation.capability_id,
            capability_version=evaluation.capability_version,
            summary=action.summary,
            reason=evaluation.verdict.summary,
            risk_categories=self._risk_categories(evaluation),
            risk_class=policy_input.risk_class,
            trust_tier=policy_input.trust_tier,
            reason_codes=list(evaluation.verdict.reason_codes),
            command_preview=policy_input.command_preview,
            cwd=policy_input.requested_cwd or str(request_cwd),
            paths=list(policy_input.requested_paths),
            network_hosts=list(policy_input.requested_network_hosts),
            requested_side_effects=list(policy_input.requested_side_effects),
            constraints=evaluation.verdict.constraints,
        )

    def resolve(
        self,
        action: PlannedAction,
        evaluation: PolicyEvaluationRecord,
        *,
        request_cwd: Path,
    ) -> tuple[CapabilityApprovalRequest, CapabilityApprovalResolution]:
        request = self.build_request(action, evaluation, request_cwd=request_cwd)
        requested_at = _utcnow()
        resolved_at = requested_at

        if self._mode is ApprovalMode.AUTO:
            return request, CapabilityApprovalResolution(
                action_id=action.id,
                capability_id=request.capability_id,
                mode=self._mode.value,
                status=ApprovalDecisionStatus.APPROVED,
                reason="Automatically approved by approval.mode=auto.",
                requested_at=requested_at,
                resolved_at=resolved_at,
                risk_categories=list(request.risk_categories),
                reason_codes=list(request.reason_codes),
                command_preview=request.command_preview,
                requested_side_effects=list(request.requested_side_effects),
            )

        if self._mode is ApprovalMode.MANUAL or self._prompt_callback is None:
            return request, CapabilityApprovalResolution(
                action_id=action.id,
                capability_id=request.capability_id,
                mode=self._mode.value,
                status=ApprovalDecisionStatus.PENDING,
                reason="Approval is required before this action can execute.",
                requested_at=requested_at,
                resolved_at=resolved_at,
                risk_categories=list(request.risk_categories),
                reason_codes=list(request.reason_codes),
                command_preview=request.command_preview,
                requested_side_effects=list(request.requested_side_effects),
            )

        approved = bool(self._prompt_callback(request))
        return request, CapabilityApprovalResolution(
            action_id=action.id,
            capability_id=request.capability_id,
            mode=self._mode.value,
            status=(ApprovalDecisionStatus.APPROVED if approved else ApprovalDecisionStatus.DENIED),
            reason=(
                "Approved by the user at the prompt."
                if approved
                else "Denied by the user at the prompt."
            ),
            requested_at=requested_at,
            resolved_at=_utcnow(),
            risk_categories=list(request.risk_categories),
            reason_codes=list(request.reason_codes),
            command_preview=request.command_preview,
            requested_side_effects=list(request.requested_side_effects),
        )

    def _risk_categories(self, evaluation: PolicyEvaluationRecord) -> list[str]:
        categories = set(evaluation.policy_input.requested_side_effects)
        categories.update(code.value for code in evaluation.verdict.reason_codes)
        return sorted(categories)
