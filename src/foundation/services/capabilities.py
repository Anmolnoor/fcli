"""Local capability store and registry for v2 Stage 1."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError

from foundation.models import (
    CapabilityConstraintSet,
    CapabilityHealth,
    CapabilityId,
    CapabilityInstallSource,
    CapabilityInvocationBudget,
    CapabilityKind,
    CapabilityManifest,
    CapabilityScopeKind,
    CapabilityScopeRule,
    CapabilityScopeTarget,
    CapabilitySideEffectMode,
    CapabilitySideEffectRule,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityTransport,
    CapabilityVersion,
    RiskClass,
    ShellAction,
    TrustTier,
)
from foundation.services.shell import ShellCommandResult
from foundation.services.tools import (
    FileDiscoveryRequest,
    FileDiscoveryResult,
    GitContextRequest,
    GitContextResult,
    HelpLookupResult,
    LocalToolService,
    SearchRequest,
    SearchResult,
    ToolAvailabilityStatus,
)

logger = logging.getLogger("foundation.services.capabilities")

SEARCH_CAPABILITY_ID = "foundation.search"
FILES_CAPABILITY_ID = "foundation.files"
GIT_CAPABILITY_ID = "foundation.git"
MAN_CAPABILITY_ID = "foundation.man"
TLDR_CAPABILITY_ID = "foundation.tldr"
SHELL_CAPABILITY_ID = "foundation.shell.command"
_BUILTIN_VERSION = "1.0.0"


class _HelpCapabilityInput(BaseModel):
    """Capability input shared by man and TLDR lookups."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    max_characters: PositiveInt = 8000


@dataclass(frozen=True, slots=True)
class CapabilityDocument:
    """One on-disk manifest document plus any parse error."""

    path: Path
    manifest: CapabilityManifest | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _BuiltinCapabilitySpec:
    capability_id: str
    name: str
    description: str
    transport: CapabilityTransport
    runtime_endpoint: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk_class: RiskClass
    declared_side_effects: tuple[str, ...]
    constraints: CapabilityConstraintSet
    required: bool = False
    binary: str | None = None

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            capability_id=CapabilityId(root=self.capability_id),
            version=CapabilityVersion(root=_BUILTIN_VERSION),
            kind=CapabilityKind.TOOL,
            name=self.name,
            description=self.description,
            transport=self.transport,
            runtime_endpoint=self.runtime_endpoint,
            transport_config={
                "binary": self.binary,
                "required": self.required,
            },
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            install_source=CapabilityInstallSource(
                kind="builtin",
                location=f"foundation://builtin/{self.capability_id}",
            ),
            owner="foundation",
            provenance="bundled",
            risk_class=self.risk_class,
            trust_tier=TrustTier.FOUNDATION,
            declared_side_effects=list(self.declared_side_effects),
            constraints=self.constraints.model_copy(deep=True),
        )


def _workspace_path_rule() -> CapabilityScopeRule:
    return CapabilityScopeRule(
        target=CapabilityScopeTarget.PATH,
        kind=CapabilityScopeKind.WORKSPACE,
    )


def _no_network_rule() -> CapabilityScopeRule:
    return CapabilityScopeRule(
        target=CapabilityScopeTarget.NETWORK,
        kind=CapabilityScopeKind.NONE,
    )


def _any_network_rule() -> CapabilityScopeRule:
    return CapabilityScopeRule(
        target=CapabilityScopeTarget.NETWORK,
        kind=CapabilityScopeKind.ANY,
    )


def _side_effect_rule(
    side_effect: str,
    mode: CapabilitySideEffectMode = CapabilitySideEffectMode.ALLOW,
) -> CapabilitySideEffectRule:
    return CapabilitySideEffectRule(side_effect=side_effect, mode=mode)


def _budget(
    *,
    timeout_seconds: int,
    output_limit_kb: int,
    max_invocations: int = 5,
    rate_limit_count: int = 10,
    rate_limit_window_seconds: int = 60,
) -> CapabilityInvocationBudget:
    return CapabilityInvocationBudget(
        timeout_seconds=timeout_seconds,
        output_limit_kb=output_limit_kb,
        max_invocations=max_invocations,
        rate_limit_count=rate_limit_count,
        rate_limit_window_seconds=rate_limit_window_seconds,
    )


_BUILTIN_CAPABILITIES: tuple[_BuiltinCapabilitySpec, ...] = (
    _BuiltinCapabilitySpec(
        capability_id=SEARCH_CAPABILITY_ID,
        name="Workspace Search",
        description="Search workspace content through the typed ripgrep wrapper.",
        transport=CapabilityTransport.BUILTIN_TOOL,
        runtime_endpoint="builtin.search",
        input_model=SearchRequest,
        output_model=SearchResult,
        risk_class=RiskClass.LOW,
        declared_side_effects=("filesystem_read",),
        constraints=CapabilityConstraintSet(
            path_rules=[_workspace_path_rule()],
            network_rules=[_no_network_rule()],
            side_effect_rules=[_side_effect_rule("filesystem_read")],
            invocation_budget=_budget(timeout_seconds=30, output_limit_kb=128),
        ),
        required=True,
        binary="rg",
    ),
    _BuiltinCapabilitySpec(
        capability_id=FILES_CAPABILITY_ID,
        name="Workspace Files",
        description="Discover workspace paths through the typed fd wrapper.",
        transport=CapabilityTransport.BUILTIN_TOOL,
        runtime_endpoint="builtin.files",
        input_model=FileDiscoveryRequest,
        output_model=FileDiscoveryResult,
        risk_class=RiskClass.LOW,
        declared_side_effects=("filesystem_read",),
        constraints=CapabilityConstraintSet(
            path_rules=[_workspace_path_rule()],
            network_rules=[_no_network_rule()],
            side_effect_rules=[_side_effect_rule("filesystem_read")],
            invocation_budget=_budget(timeout_seconds=30, output_limit_kb=128),
        ),
        required=False,
        binary="fd",
    ),
    _BuiltinCapabilitySpec(
        capability_id=GIT_CAPABILITY_ID,
        name="Git Context",
        description="Inspect repository status, diff, branch, and recent commits.",
        transport=CapabilityTransport.BUILTIN_TOOL,
        runtime_endpoint="builtin.git",
        input_model=GitContextRequest,
        output_model=GitContextResult,
        risk_class=RiskClass.LOW,
        declared_side_effects=("filesystem_read",),
        constraints=CapabilityConstraintSet(
            path_rules=[_workspace_path_rule()],
            network_rules=[_no_network_rule()],
            side_effect_rules=[_side_effect_rule("filesystem_read")],
            invocation_budget=_budget(timeout_seconds=30, output_limit_kb=128),
        ),
        required=True,
        binary="git",
    ),
    _BuiltinCapabilitySpec(
        capability_id=MAN_CAPABILITY_ID,
        name="Manual Pages",
        description="Read local manual pages through the typed help wrapper.",
        transport=CapabilityTransport.BUILTIN_TOOL,
        runtime_endpoint="builtin.man",
        input_model=_HelpCapabilityInput,
        output_model=HelpLookupResult,
        risk_class=RiskClass.LOW,
        declared_side_effects=("local_help_read",),
        constraints=CapabilityConstraintSet(
            network_rules=[_no_network_rule()],
            side_effect_rules=[_side_effect_rule("local_help_read")],
            invocation_budget=_budget(timeout_seconds=30, output_limit_kb=16),
        ),
        required=False,
        binary="man",
    ),
    _BuiltinCapabilitySpec(
        capability_id=TLDR_CAPABILITY_ID,
        name="TLDR Pages",
        description="Read local TLDR help pages through the typed help wrapper.",
        transport=CapabilityTransport.BUILTIN_TOOL,
        runtime_endpoint="builtin.tldr",
        input_model=_HelpCapabilityInput,
        output_model=HelpLookupResult,
        risk_class=RiskClass.LOW,
        declared_side_effects=("local_help_read",),
        constraints=CapabilityConstraintSet(
            network_rules=[_no_network_rule()],
            side_effect_rules=[_side_effect_rule("local_help_read")],
            invocation_budget=_budget(timeout_seconds=30, output_limit_kb=16),
        ),
        required=False,
        binary="tldr",
    ),
    _BuiltinCapabilitySpec(
        capability_id=SHELL_CAPABILITY_ID,
        name="Shell Runtime",
        description="Execute one shell command through the Foundation runtime.",
        transport=CapabilityTransport.SHELL_RUNTIME,
        runtime_endpoint="builtin.shell",
        input_model=ShellAction,
        output_model=ShellCommandResult,
        risk_class=RiskClass.MEDIUM,
        declared_side_effects=(
            "filesystem_read",
            "workspace_write",
            "destructive",
            "network",
            "environment",
            "permission",
            "recursive",
            "subprocess",
            "unknown",
        ),
        constraints=CapabilityConstraintSet(
            path_rules=[_workspace_path_rule()],
            network_rules=[_any_network_rule()],
            side_effect_rules=[
                _side_effect_rule("filesystem_read"),
                _side_effect_rule("workspace_write", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("destructive", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("network", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("environment", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("permission", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("recursive", CapabilitySideEffectMode.REQUIRE_APPROVAL),
                _side_effect_rule("subprocess"),
                _side_effect_rule("unknown", CapabilitySideEffectMode.REQUIRE_APPROVAL),
            ],
            invocation_budget=_budget(timeout_seconds=300, output_limit_kb=256),
        ),
    ),
)


def _utcnow() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def _version_key(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".", 2)
    return int(major), int(minor), int(patch)


def _builtin_manifest(
    spec: _BuiltinCapabilitySpec,
    *,
    existing: CapabilityManifest | None = None,
) -> CapabilityManifest:
    builtin = spec.manifest()
    if existing is None:
        return builtin
    return builtin.model_copy(
        update={
            "state": existing.state,
            "installed_at": existing.installed_at,
        }
    )


class CapabilityStore:
    """Filesystem-backed local store for installed capability manifests."""

    def __init__(self, root: Path, *, create_root: bool = True) -> None:
        self._root = Path(root).expanduser().resolve()
        if self._root.exists() and not self._root.is_dir():
            raise NotADirectoryError(f"Capability store root is not a directory: {self._root}")
        if create_root:
            self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the canonical capability store root."""
        return self._root

    def manifest_path(self, capability_id: str, version: str) -> Path:
        """Return the on-disk manifest path for one capability version."""
        return self._root / capability_id / version / "manifest.json"

    def write_manifest(self, manifest: CapabilityManifest) -> Path:
        """Persist one validated manifest version into the local store."""
        path = self.manifest_path(manifest.id, str(manifest.version))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json_dumps(manifest.model_dump(mode="json")), encoding="utf-8")
        return path

    def delete_manifest(self, capability_id: str, version: str) -> None:
        """Remove one manifest version from the local store."""
        path = self.manifest_path(capability_id, version)
        if path.exists():
            path.unlink()
        version_dir = path.parent
        capability_dir = version_dir.parent
        if version_dir.exists() and not any(version_dir.iterdir()):
            version_dir.rmdir()
        if capability_dir.exists() and not any(capability_dir.iterdir()):
            capability_dir.rmdir()

    def list_documents(self) -> list[CapabilityDocument]:
        """Return all manifest documents with validation errors preserved."""
        if not self._root.exists():
            return []
        documents: list[CapabilityDocument] = []
        for path in sorted(self._root.rglob("manifest.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                documents.append(CapabilityDocument(path=path, manifest=None, error=str(exc)))
                continue
            try:
                manifest = CapabilityManifest.model_validate(payload)
            except ValidationError as exc:
                documents.append(CapabilityDocument(path=path, manifest=None, error=str(exc)))
                continue
            documents.append(CapabilityDocument(path=path, manifest=manifest))
        return documents

    def list_manifests(self) -> list[CapabilityManifest]:
        """Return all manifest versions that validated successfully."""
        return [
            document.manifest
            for document in self.list_documents()
            if document.manifest is not None
        ]


class CapabilityResolver:
    """Resolve one manifest version from the local capability store."""

    def __init__(self, store: CapabilityStore) -> None:
        self._store = store

    def resolve(
        self,
        capability_id: str,
        version: str | None = None,
    ) -> CapabilityManifest | None:
        """Resolve one capability by id and optional semantic version."""
        matches = [
            manifest
            for manifest in self._store.list_manifests()
            if manifest.id == capability_id
        ]
        if not matches:
            return None
        if version is not None:
            for manifest in matches:
                if str(manifest.version) == version:
                    return manifest
            return None
        return sorted(
            matches,
            key=lambda manifest: _version_key(str(manifest.version)),
            reverse=True,
        )[0]


class CapabilityRegistry:
    """Registry service that manages lifecycle, health, and planner snapshots."""

    def __init__(
        self,
        *,
        store: CapabilityStore,
        tool_service: LocalToolService,
        read_only: bool = False,
    ) -> None:
        self._store = store
        self._tool_service = tool_service
        self._read_only = read_only
        self._resolver = CapabilityResolver(store)
        if not self._read_only:
            self._seed_builtin_capabilities()

    @property
    def store(self) -> CapabilityStore:
        """Return the backing store used by the registry."""
        return self._store

    def register(self, manifest: CapabilityManifest) -> CapabilityManifest:
        """Validate, health-check, and persist one capability manifest."""
        self._assert_mutable()
        timestamp = _utcnow()
        payload = manifest.model_dump(mode="json")
        payload.update(
            {
                "installed_at": manifest.installed_at or timestamp,
                "updated_at": timestamp,
            }
        )
        candidate = CapabilityManifest.model_validate(payload)
        refreshed = self._with_health(candidate)
        self._store.write_manifest(refreshed)
        return refreshed

    def list_capabilities(self, *, include_disabled: bool = True) -> list[CapabilityManifest]:
        """List installed capabilities ordered by id then descending version."""
        manifests: list[CapabilityManifest] = []
        for manifest in self._manifest_inventory():
            refreshed = self._with_health(manifest)
            if not include_disabled and refreshed.state is CapabilityState.DISABLED:
                continue
            manifests.append(refreshed)
        return sorted(
            manifests,
            key=lambda manifest: (
                manifest.id,
                -_version_key(str(manifest.version))[0],
                -_version_key(str(manifest.version))[1],
                -_version_key(str(manifest.version))[2],
            ),
        )

    def invalid_manifests(self) -> list[CapabilityDocument]:
        """Return store entries that failed manifest validation."""
        return [
            document
            for document in self._store.list_documents()
            if document.manifest is None
        ]

    def resolve(
        self,
        capability_id: str,
        version: str | None = None,
        *,
        allow_disabled: bool = False,
        allow_unhealthy: bool = False,
    ) -> CapabilityManifest:
        """Resolve one installed capability and enforce lifecycle state."""
        matches = [
            manifest
            for manifest in self._manifest_inventory()
            if manifest.id == capability_id
        ]
        manifest = None
        if matches:
            if version is not None:
                for candidate in matches:
                    if str(candidate.version) == version:
                        manifest = candidate
                        break
            else:
                manifest = sorted(
                    matches,
                    key=lambda candidate: _version_key(str(candidate.version)),
                    reverse=True,
                )[0]
        if manifest is None:
            raise ValueError(f"No capability found for {capability_id!r}.")
        refreshed = self._with_health(manifest)
        if refreshed.state is CapabilityState.DISABLED and not allow_disabled:
            raise ValueError(f"Capability {capability_id!r} is disabled.")
        if refreshed.health is not CapabilityHealth.HEALTHY and not allow_unhealthy:
            detail = refreshed.health_detail or "Capability is not healthy."
            raise ValueError(f"Capability {capability_id!r} is unavailable: {detail}")
        return refreshed

    def enable(self, capability_id: str, version: str | None = None) -> CapabilityManifest:
        """Enable one installed capability version."""
        self._assert_mutable()
        manifest = self.resolve(
            capability_id,
            version,
            allow_disabled=True,
            allow_unhealthy=True,
        )
        updated = manifest.model_copy(
            update={
                "state": CapabilityState.ENABLED,
                "updated_at": _utcnow(),
            }
        )
        return self.register(updated)

    def disable(self, capability_id: str, version: str | None = None) -> CapabilityManifest:
        """Disable one installed capability version."""
        self._assert_mutable()
        manifest = self.resolve(
            capability_id,
            version,
            allow_disabled=True,
            allow_unhealthy=True,
        )
        updated = manifest.model_copy(
            update={
                "state": CapabilityState.DISABLED,
                "updated_at": _utcnow(),
            }
        )
        self._store.write_manifest(updated)
        return updated

    def remove(self, capability_id: str, version: str | None = None) -> CapabilityManifest:
        """Remove one installed capability version from the local store."""
        self._assert_mutable()
        manifest = self.resolve(
            capability_id,
            version,
            allow_disabled=True,
            allow_unhealthy=True,
        )
        self._store.delete_manifest(manifest.id, str(manifest.version))
        return manifest

    def planner_snapshot(self) -> list[CapabilitySnapshot]:
        """Return only enabled and healthy capabilities for planner context."""
        snapshots: list[CapabilitySnapshot] = []
        for manifest in self.list_capabilities(include_disabled=False):
            if manifest.health is CapabilityHealth.HEALTHY:
                snapshots.append(CapabilitySnapshot.from_manifest(manifest))
        return snapshots

    def _seed_builtin_capabilities(self) -> None:
        for spec in _BUILTIN_CAPABILITIES:
            existing = self._resolver.resolve(spec.capability_id, _BUILTIN_VERSION)
            builtin = _builtin_manifest(spec, existing=existing)
            self.register(builtin)

    def _with_health(self, manifest: CapabilityManifest) -> CapabilityManifest:
        health, detail = self._health_check(manifest)
        if manifest.health is health and manifest.health_detail == detail:
            return manifest
        updated = manifest.model_copy(
            update={
                "health": health,
                "health_detail": detail,
                "updated_at": _utcnow(),
            }
        )
        if not self._read_only:
            self._store.write_manifest(updated)
        return updated

    def _assert_mutable(self) -> None:
        if self._read_only:
            raise RuntimeError("Capability registry is read-only.")

    def _manifest_inventory(self) -> list[CapabilityManifest]:
        manifests = self._store.list_manifests()
        manifest_by_key = {
            (manifest.id, str(manifest.version)): manifest for manifest in manifests
        }
        builtin_keys: set[tuple[str, str]] = set()
        inventory: list[CapabilityManifest] = []

        for spec in _BUILTIN_CAPABILITIES:
            key = (spec.capability_id, _BUILTIN_VERSION)
            builtin_keys.add(key)
            inventory.append(_builtin_manifest(spec, existing=manifest_by_key.get(key)))

        for manifest in manifests:
            if (manifest.id, str(manifest.version)) in builtin_keys:
                continue
            inventory.append(manifest)

        return inventory

    def _health_check(self, manifest: CapabilityManifest) -> tuple[CapabilityHealth, str | None]:
        if manifest.transport is CapabilityTransport.SHELL_RUNTIME:
            if manifest.runtime_endpoint != "builtin.shell":
                return CapabilityHealth.UNHEALTHY, "Shell runtime endpoint is not recognized."
            return CapabilityHealth.HEALTHY, "Shell runtime is available."

        if manifest.transport is CapabilityTransport.BUILTIN_TOOL:
            binary_name = manifest.transport_config.get("binary")
            if not isinstance(binary_name, str) or not binary_name:
                return CapabilityHealth.UNHEALTHY, "Built-in tool manifests must declare a binary."
            availability = {
                item.name: item for item in self._tool_service.availability_report()
            }
            item = availability.get(binary_name)
            if item is None:
                return CapabilityHealth.UNHEALTHY, f"Binary {binary_name!r} is not tracked."
            if item.status is not ToolAvailabilityStatus.AVAILABLE:
                detail = item.install_hint or f"Binary {binary_name!r} is missing."
                return CapabilityHealth.UNHEALTHY, detail
            return CapabilityHealth.HEALTHY, f"Resolved {binary_name} at {item.path}."

        if manifest.transport is CapabilityTransport.EXTERNAL_SERVICE:
            return (
                CapabilityHealth.UNHEALTHY,
                "External-service capabilities are not executable in Stage 1.",
            )

        return CapabilityHealth.UNKNOWN, None
