"""Typed capability registry and policy models for v2 Stage 2."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, RootModel, field_validator

_CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class StrictModel(BaseModel):
    """Base model that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class CapabilityId(RootModel[str]):
    """Normalized capability identifier."""

    @field_validator("root")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _CAPABILITY_ID_RE.fullmatch(normalized):
            raise ValueError("Capability ids must match ^[a-z0-9][a-z0-9._-]{0,63}$.")
        return normalized

    def __str__(self) -> str:
        return self.root


class CapabilityVersion(RootModel[str]):
    """Semantic version for one capability manifest."""

    @field_validator("root")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        normalized = value.strip()
        if not _SEMVER_RE.fullmatch(normalized):
            raise ValueError("Capability versions must use semantic version format x.y.z.")
        return normalized

    def __str__(self) -> str:
        return self.root


class CapabilityKind(StrEnum):
    """High-level capability taxonomy."""

    TOOL = "tool"
    SKILL = "skill"


class CapabilityTransport(StrEnum):
    """Runtime transport used to execute or reach a capability."""

    BUILTIN_TOOL = "builtin_tool"
    SHELL_RUNTIME = "shell_runtime"
    EXTERNAL_SERVICE = "external_service"


class CapabilityState(StrEnum):
    """Lifecycle state for one installed capability version."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class CapabilityHealth(StrEnum):
    """Runtime health state for one installed capability version."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class RiskClass(StrEnum):
    """Planner-facing risk classification for one capability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TrustTier(StrEnum):
    """Trust tier associated with one capability."""

    FOUNDATION = "foundation"
    USER = "user"
    EXTERNAL = "external"


class CapabilityScopeTarget(StrEnum):
    """Scope families enforced by the policy layer."""

    PATH = "path"
    NETWORK = "network"


class CapabilityScopeKind(StrEnum):
    """Scope-matching strategies supported by capability policy metadata."""

    NONE = "none"
    WORKSPACE = "workspace"
    REQUEST_CWD = "request_cwd"
    PREFIX = "prefix"
    EXACT = "exact"
    ANY = "any"


class CapabilitySideEffectMode(StrEnum):
    """Policy posture for one declared side effect."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class CapabilityPolicyOutcome(StrEnum):
    """Executor-facing verdicts for one capability invocation."""

    ALLOW = "allow"
    ALLOW_WITH_CONSTRAINTS = "allow_with_constraints"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class PolicyReasonCode(StrEnum):
    """Machine-readable explanations for one policy verdict."""

    CAPABILITY_DISABLED = "capability_disabled"
    CAPABILITY_UNHEALTHY = "capability_unhealthy"
    UNTRUSTED_CAPABILITY = "untrusted_capability"
    HIGH_RISK_CAPABILITY = "high_risk_capability"
    MODEL_MARKED_APPROVAL = "model_marked_approval"
    SIDE_EFFECT_REQUIRES_APPROVAL = "side_effect_requires_approval"
    UNDECLARED_SIDE_EFFECT = "undeclared_side_effect"
    PATH_OUT_OF_SCOPE = "path_out_of_scope"
    SCOPE_ESCALATION = "scope_escalation"
    NETWORK_OUT_OF_SCOPE = "network_out_of_scope"
    INVOCATION_LIMIT_EXCEEDED = "invocation_limit_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    INVALID_INVOCATION = "invalid_invocation"


class CapabilityScopeRule(StrictModel):
    """One declared scope rule for path or network access."""

    target: CapabilityScopeTarget
    kind: CapabilityScopeKind
    value: str | None = None

    @field_validator("value")
    @classmethod
    def _normalize_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class CapabilitySideEffectRule(StrictModel):
    """One declared side effect plus the required policy posture."""

    side_effect: str = Field(min_length=1)
    mode: CapabilitySideEffectMode = CapabilitySideEffectMode.ALLOW

    @field_validator("side_effect")
    @classmethod
    def _normalize_side_effect(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Declared side effects must not be empty.")
        return normalized


class CapabilityInvocationBudget(StrictModel):
    """Per-invocation limits enforced by the executor."""

    timeout_seconds: PositiveInt | None = None
    output_limit_kb: PositiveInt | None = None
    max_invocations: PositiveInt | None = None
    rate_limit_count: PositiveInt | None = None
    rate_limit_window_seconds: PositiveInt | None = None


class CapabilityConstraintSet(StrictModel):
    """Resolved constraints attached to capability metadata or one verdict."""

    path_rules: list[CapabilityScopeRule] = Field(default_factory=list)
    network_rules: list[CapabilityScopeRule] = Field(default_factory=list)
    side_effect_rules: list[CapabilitySideEffectRule] = Field(default_factory=list)
    invocation_budget: CapabilityInvocationBudget | None = None


class CapabilityPolicyInput(StrictModel):
    """Normalized policy input assembled from manifest metadata and invocation context."""

    action_id: str = Field(min_length=1)
    capability_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
    )
    capability_version: str = Field(
        min_length=1,
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
    )
    capability_kind: CapabilityKind
    transport: CapabilityTransport
    runtime_endpoint: str = Field(min_length=1)
    trust_tier: TrustTier
    risk_class: RiskClass
    capability_state: CapabilityState
    capability_health: CapabilityHealth
    approval_mode: str = Field(min_length=1)
    request_cwd: str = Field(min_length=1)
    requested_cwd: str | None = None
    command_preview: str | None = None
    planner_requires_approval: bool = False
    planner_approval_reason: str | None = None
    requested_paths: list[str] = Field(default_factory=list)
    requested_network_hosts: list[str] = Field(default_factory=list)
    requested_side_effects: list[str] = Field(default_factory=list)
    requested_timeout_seconds: PositiveInt | None = None
    requested_output_limit_kb: PositiveInt | None = None
    invocation_count: int = Field(default=0, ge=0)
    prior_invocations_in_window: int = Field(default=0, ge=0)
    constraints: CapabilityConstraintSet = Field(default_factory=CapabilityConstraintSet)

    @field_validator("requested_paths", "requested_network_hosts", mode="before")
    @classmethod
    def _normalize_requested_values(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            raise TypeError("Requested scope values must be a list or tuple.")
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("requested_side_effects", mode="before")
    @classmethod
    def _normalize_requested_side_effects(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            raise TypeError("Requested side effects must be a list or tuple.")
        normalized: list[str] = []
        for item in value:
            effect = str(item).strip().lower()
            if effect:
                normalized.append(effect)
        return normalized


class CapabilityPolicyVerdict(StrictModel):
    """Typed verdict returned by the Stage 2 capability policy engine."""

    outcome: CapabilityPolicyOutcome
    summary: str = Field(min_length=1)
    reason_codes: list[PolicyReasonCode] = Field(default_factory=list)
    constraints: CapabilityConstraintSet | None = None


class PolicyEvaluationRecord(StrictModel):
    """Persistable policy evaluation record for one capability invocation."""

    action_id: str = Field(min_length=1)
    capability_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
    )
    capability_version: str = Field(
        min_length=1,
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
    )
    verdict: CapabilityPolicyVerdict
    policy_input: CapabilityPolicyInput
    evaluated_at: str = Field(min_length=1)


class CapabilityInstallSource(StrictModel):
    """Provenance for how one capability entered the local store."""

    kind: str = Field(min_length=1)
    location: str = Field(min_length=1)
    reference: str | None = None


class CapabilityManifest(StrictModel):
    """Typed manifest for one installed capability version."""

    capability_id: CapabilityId
    version: CapabilityVersion
    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    transport: CapabilityTransport
    runtime_endpoint: str = Field(min_length=1)
    transport_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    install_source: CapabilityInstallSource
    owner: str = Field(min_length=1)
    provenance: str | None = None
    risk_class: RiskClass
    trust_tier: TrustTier
    declared_side_effects: list[str] = Field(default_factory=list)
    constraints: CapabilityConstraintSet = Field(default_factory=CapabilityConstraintSet)
    state: CapabilityState = CapabilityState.ENABLED
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    health_detail: str | None = None
    installed_at: str | None = None
    updated_at: str | None = None

    @field_validator("declared_side_effects")
    @classmethod
    def _normalize_side_effects(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            effect = item.strip().lower()
            if not effect:
                raise ValueError("Declared side effects must not be empty.")
            normalized.append(effect)
        return normalized

    @field_validator("input_schema")
    @classmethod
    def _validate_input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("Capability manifests must declare an input schema.")
        return value

    @property
    def id(self) -> str:
        """Return the normalized capability id as a string."""
        return str(self.capability_id)


class CapabilitySnapshot(StrictModel):
    """Planner-facing snapshot for one enabled and healthy capability."""

    capability_id: CapabilityId
    version: CapabilityVersion
    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    transport: CapabilityTransport
    runtime_endpoint: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    risk_class: RiskClass
    trust_tier: TrustTier
    declared_side_effects: list[str] = Field(default_factory=list)
    constraints: CapabilityConstraintSet = Field(default_factory=CapabilityConstraintSet)

    @classmethod
    def from_manifest(cls, manifest: CapabilityManifest) -> CapabilitySnapshot:
        """Create a planner snapshot from one installed manifest."""
        return cls(
            capability_id=manifest.capability_id,
            version=manifest.version,
            kind=manifest.kind,
            name=manifest.name,
            description=manifest.description,
            transport=manifest.transport,
            runtime_endpoint=manifest.runtime_endpoint,
            input_schema=dict(manifest.input_schema),
            output_schema=manifest.output_schema,
            risk_class=manifest.risk_class,
            trust_tier=manifest.trust_tier,
            declared_side_effects=list(manifest.declared_side_effects),
            constraints=manifest.constraints.model_copy(deep=True),
        )
