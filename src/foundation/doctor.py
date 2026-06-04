"""Doctor checks for Foundation CLI environment and config readiness."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from foundation.models import CapabilityHealth, CapabilityState
from foundation.observability import emit_event
from foundation.services import CapabilityRegistry, CapabilityStore, LocalToolService
from foundation.services.history import HistoryStore
from foundation.settings import (
    AppSettings,
    SecretResolutionStatus,
    SettingsLoadError,
    load_settings,
)


class DoctorStatus(StrEnum):
    """Supported doctor outcomes."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class DoctorCheck(BaseModel):
    """One doctor check result."""

    name: str
    status: DoctorStatus
    summary: str
    detail: str | None = None


class DoctorReport(BaseModel):
    """Aggregated doctor results."""

    checks: list[DoctorCheck] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the report contains no failures."""
        return all(check.status is not DoctorStatus.FAIL for check in self.checks)

    @property
    def exit_code(self) -> int:
        """Return the process exit code for the report."""
        return 0 if self.ok else 1


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return candidate


def _python_version_check() -> DoctorCheck:
    major = sys.version_info.major
    minor = sys.version_info.minor
    version_text = f"{major}.{minor}.{sys.version_info.micro}"

    if major == 3 and minor == 12:
        return DoctorCheck(
            name="Python version",
            status=DoctorStatus.PASS,
            summary=f"Running supported Python {version_text}.",
        )

    return DoctorCheck(
        name="Python version",
        status=DoctorStatus.FAIL,
        summary=f"Python {version_text} is unsupported.",
        detail="Foundation CLI Stage 2 requires Python 3.12.x.",
    )


def _config_check(settings: AppSettings) -> DoctorCheck:
    if settings.config_exists:
        return DoctorCheck(
            name="Config readability",
            status=DoctorStatus.PASS,
            summary=f"Loaded config from {settings.config_path}.",
        )

    return DoctorCheck(
        name="Config readability",
        status=DoctorStatus.PASS,
        summary=f"No config file found at {settings.config_path}; defaults are active.",
    )


def _required_directories_check(settings: AppSettings) -> DoctorCheck:
    locations = {
        "data_dir": settings.app.data_dir,
        "state_dir": settings.app.state_dir,
        "log_dir": settings.app.log_dir,
        "history_parent": settings.history.database_path.parent,
    }
    ready: list[str] = []
    missing: list[str] = []
    blocked: list[str] = []

    for label, path in locations.items():
        if path.exists():
            if path.is_dir():
                ready.append(f"{label}: {path}")
            else:
                blocked.append(f"{label}: {path} exists but is not a directory")
            continue

        parent = _nearest_existing_parent(path)
        if os.access(parent, os.W_OK):
            missing.append(f"{label}: {path} is missing but creatable from {parent}")
        else:
            blocked.append(f"{label}: {path} cannot be created because {parent} is not writable")

    if blocked:
        return DoctorCheck(
            name="Required directories",
            status=DoctorStatus.FAIL,
            summary="One or more required directories are blocked.",
            detail="\n".join(blocked + missing + ready),
        )

    if missing:
        return DoctorCheck(
            name="Required directories",
            status=DoctorStatus.WARN,
            summary="Some required directories are missing but creatable.",
            detail="\n".join(missing + ready),
        )

    return DoctorCheck(
        name="Required directories",
        status=DoctorStatus.PASS,
        summary="All required directories are present.",
        detail="\n".join(ready),
    )


def _provider_readiness_check(settings: AppSettings) -> DoctorCheck:
    provider_name = settings.provider.normalized_name()
    credential_sources = ", ".join(settings.provider.credential_source_order()) or "none"
    if not provider_name:
        return DoctorCheck(
            name="Provider readiness",
            status=DoctorStatus.FAIL,
            summary="Provider name is missing.",
            detail="Set [provider].name to a supported provider.",
        )
    if provider_name not in {"codex", "openai", "ollama"}:
        return DoctorCheck(
            name="Provider readiness",
            status=DoctorStatus.FAIL,
            summary=f"Provider {settings.provider.name!r} is not supported.",
            detail="Supported providers: codex, openai, ollama.",
        )
    if not settings.provider.model.strip():
        return DoctorCheck(
            name="Provider readiness",
            status=DoctorStatus.FAIL,
            summary="Provider model is empty.",
            detail="Set [provider].model to a non-empty value.",
        )
    return DoctorCheck(
        name="Provider readiness",
        status=DoctorStatus.PASS,
        summary="Provider configuration is supported.",
        detail=(
            f"Provider: {settings.provider.name}\n"
            f"Model: {settings.provider.model}\n"
            f"Base URL: {settings.provider.effective_base_url()}\n"
            f"Request timeout: {settings.provider.request_timeout_seconds}s\n"
            f"Credentials required: {'yes' if settings.provider.credentials_required() else 'no'}\n"
            f"Credential sources: {credential_sources}"
        ),
    )


def _database_health_check(settings: AppSettings) -> DoctorCheck:
    try:
        store = HistoryStore(
            database_path=settings.history.database_path,
            retention_days=settings.history.retention_days,
            max_entries=settings.history.max_entries,
        )
        store.list_sessions(limit=1)
    except Exception as exc:
        return DoctorCheck(
            name="History database",
            status=DoctorStatus.FAIL,
            summary="History database is not queryable.",
            detail=f"{settings.history.database_path}: {exc}",
        )

    return DoctorCheck(
        name="History database",
        status=DoctorStatus.PASS,
        summary="History database is available and queryable.",
        detail=f"Database path: {settings.history.database_path}",
    )


def _log_path_check(settings: AppSettings) -> DoctorCheck:
    log_file = settings.app.log_dir / "foundation.log"
    if settings.app.log_dir.exists():
        if not settings.app.log_dir.is_dir():
            return DoctorCheck(
                name="Log path",
                status=DoctorStatus.FAIL,
                summary="Configured log directory is not a directory.",
                detail=f"{settings.app.log_dir} exists but is not a directory.",
            )
        if not os.access(settings.app.log_dir, os.W_OK):
            return DoctorCheck(
                name="Log path",
                status=DoctorStatus.FAIL,
                summary="Configured log directory is not writable.",
                detail=f"Cannot write logs under {settings.app.log_dir}.",
            )
        if log_file.exists() and not log_file.is_file():
            return DoctorCheck(
                name="Log path",
                status=DoctorStatus.WARN,
                summary="Expected log file path exists but is not a regular file.",
                detail=f"Expected a file at {log_file}.",
            )
        return DoctorCheck(
            name="Log path",
            status=DoctorStatus.PASS,
            summary="Log path is writable.",
            detail=f"Log file target: {log_file}",
        )

    parent = _nearest_existing_parent(settings.app.log_dir)
    if os.access(parent, os.W_OK):
        return DoctorCheck(
            name="Log path",
            status=DoctorStatus.WARN,
            summary="Log directory is missing but creatable.",
            detail=f"Expected log directory: {settings.app.log_dir}",
        )
    return DoctorCheck(
        name="Log path",
        status=DoctorStatus.FAIL,
        summary="Log directory is blocked by filesystem permissions.",
        detail=f"Cannot create log directory under {parent}.",
    )


def _events_log_check(settings: AppSettings) -> DoctorCheck:
    if not settings.monitor.enabled:
        return DoctorCheck(
            name="Event log",
            status=DoctorStatus.WARN,
            summary="Persistent event log is disabled in settings.",
            detail="Set monitor.enabled=true (or omit) to enable the NDJSON event log.",
        )
    events_dir = settings.monitor.events_dir
    retention = settings.monitor.retention
    detail_lines = [
        f"events_dir: {events_dir}",
        f"retention: max_sessions={retention.max_sessions} max_bytes={retention.max_bytes}",
    ]
    transports = settings.monitor.live_transports
    if transports:
        detail_lines.append(f"live transports configured: {','.join(transports)}")
        if "unix" in transports and settings.monitor.socket_path is not None:
            detail_lines.append(f"socket_path: {settings.monitor.socket_path}")
        if "http" in transports and settings.monitor.http_port is not None:
            detail_lines.append(f"http_port: {settings.monitor.http_port}")
    else:
        detail_lines.append(
            "live transports: none (use --monitor-socket or --monitor-http to enable)"
        )
    session_count = 0
    total_bytes = 0
    if events_dir.exists():
        if not events_dir.is_dir():
            return DoctorCheck(
                name="Event log",
                status=DoctorStatus.FAIL,
                summary="Configured events directory is not a directory.",
                detail=f"{events_dir} exists but is not a directory.",
            )
        if not os.access(events_dir, os.W_OK):
            return DoctorCheck(
                name="Event log",
                status=DoctorStatus.FAIL,
                summary="Events directory is not writable.",
                detail=f"Cannot write event logs under {events_dir}.",
            )
        for entry in events_dir.glob("*.ndjson"):
            session_count += 1
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                continue
        detail_lines.append(f"current usage: sessions={session_count} bytes={total_bytes}")
        return DoctorCheck(
            name="Event log",
            status=DoctorStatus.PASS,
            summary="Persistent event log directory is writable.",
            detail="\n".join(detail_lines),
        )
    parent = _nearest_existing_parent(events_dir)
    if os.access(parent, os.W_OK):
        return DoctorCheck(
            name="Event log",
            status=DoctorStatus.WARN,
            summary="Events directory is missing but creatable.",
            detail="\n".join(detail_lines),
        )
    return DoctorCheck(
        name="Event log",
        status=DoctorStatus.FAIL,
        summary="Events directory is blocked by filesystem permissions.",
        detail=f"Cannot create events directory under {parent}.",
    )


def _secret_lookup_check(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorCheck:
    if settings.provider.normalized_name() == "codex":
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.PASS,
            summary="Provider uses local Codex ChatGPT login; no OpenAI API key is required.",
            detail="Credential sources: codex:chatgpt-login",
        )

    resolution = settings.provider.resolve_api_key(
        environment=settings.provider_environment(environment),
    )
    credential_sources = ", ".join(settings.provider.credential_source_order()) or "none"
    credentials_required = settings.provider.credentials_required()

    if resolution.status is SecretResolutionStatus.RESOLVED:
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.PASS,
            summary=resolution.detail,
            detail=f"Credential sources: {credential_sources}",
        )

    if resolution.status is SecretResolutionStatus.UNAVAILABLE:
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.FAIL,
            summary="Provider credentials could not be checked through the keychain.",
            detail=f"{resolution.detail}\nCredential sources: {credential_sources}",
        )

    if not credentials_required:
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.PASS,
            summary="Provider credentials are optional for the configured provider endpoint.",
            detail=f"{resolution.detail}\nCredential sources: {credential_sources}",
        )

    return DoctorCheck(
        name="Secret lookup health",
        status=DoctorStatus.FAIL,
        summary="Provider credentials are not configured.",
        detail=f"{resolution.detail}\nCredential sources: {credential_sources}",
    )


def _capability_registry_check(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorCheck:
    store_root = settings.app.data_dir / "capabilities"
    detail_lines: list[str] = []
    store_missing_but_creatable = False

    if store_root.exists():
        if not store_root.is_dir():
            return DoctorCheck(
                name="Capability registry",
                status=DoctorStatus.FAIL,
                summary="Capability store root is not a directory.",
                detail=f"{store_root} exists but is not a directory.",
            )
    else:
        parent = _nearest_existing_parent(store_root)
        if not os.access(parent, os.W_OK):
            return DoctorCheck(
                name="Capability registry",
                status=DoctorStatus.FAIL,
                summary="Capability store is blocked by filesystem permissions.",
                detail=f"Cannot create capability store under {parent}.",
            )
        store_missing_but_creatable = True
        detail_lines.append(
            f"Capability store root: {store_root} is missing but creatable from {parent}."
        )

    service = LocalToolService(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=min(settings.shell.default_timeout_seconds, 30),
        capture_limit_kb=settings.shell.capture_limit_kb,
        environment=environment,
    )
    try:
        registry = CapabilityRegistry(
            store=CapabilityStore(store_root, create_root=False),
            tool_service=service,
            read_only=True,
        )
        capabilities = registry.list_capabilities()
        invalid_documents = registry.invalid_manifests()
    except Exception as exc:
        return DoctorCheck(
            name="Capability registry",
            status=DoctorStatus.FAIL,
            summary="Capability registry could not be inspected.",
            detail=str(exc),
        )

    failed_required: list[str] = []
    warned_optional: list[str] = []

    for document in invalid_documents:
        failed_required.append(f"Invalid manifest: {document.path}")
        detail_lines.append(f"{document.path}: invalid manifest | {document.error}")

    for capability in capabilities:
        required = bool(capability.transport_config.get("required", False))
        status = capability.health.value
        line = f"{capability.id}@{capability.version}: {capability.state.value}, {status}"
        boundary_parts = [
            f"risk={capability.risk_class.value}",
            f"trust={capability.trust_tier.value}",
        ]
        if capability.declared_side_effects:
            boundary_parts.append(
                "side_effects=" + ",".join(sorted(capability.declared_side_effects))
            )
        line = f"{line} [{'; '.join(boundary_parts)}]"
        if capability.health_detail:
            line = f"{line} | {capability.health_detail}"
        detail_lines.append(line)
        if capability.state is CapabilityState.DISABLED and required:
            failed_required.append(f"{capability.id} is disabled.")
            continue
        if capability.health is CapabilityHealth.HEALTHY:
            continue
        if required:
            failed_required.append(f"{capability.id} is unhealthy.")
        else:
            warned_optional.append(f"{capability.id} is unhealthy.")

    if failed_required:
        return DoctorCheck(
            name="Capability registry",
            status=DoctorStatus.FAIL,
            summary="Required capabilities are invalid, disabled, or unhealthy.",
            detail="\n".join(detail_lines),
        )

    if warned_optional:
        summary = "Some optional capabilities are unavailable."
        if store_missing_but_creatable:
            summary = "Capability store is missing but creatable."
        return DoctorCheck(
            name="Capability registry",
            status=DoctorStatus.WARN,
            summary=summary,
            detail="\n".join(detail_lines),
        )

    if store_missing_but_creatable:
        return DoctorCheck(
            name="Capability registry",
            status=DoctorStatus.WARN,
            summary="Capability store is missing but creatable.",
            detail="\n".join(detail_lines),
        )

    return DoctorCheck(
        name="Capability registry",
        status=DoctorStatus.PASS,
        summary="Enabled capabilities are healthy and queryable.",
        detail="\n".join(detail_lines),
    )


def run_doctor(
    config_path: Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run the current Foundation CLI doctor checks."""
    checks = [_python_version_check()]

    try:
        settings = load_settings(config_path=config_path, overrides=overrides)
    except SettingsLoadError as exc:
        checks.append(
            DoctorCheck(
                name="Config readability",
                status=DoctorStatus.FAIL,
                summary="Configuration could not be loaded.",
                detail=str(exc),
            )
        )
        return DoctorReport(checks=checks)

    emit_event(
        "doctor_check_start",
        payload={
            "config_path": str(settings.config_path),
            "provider_name": settings.provider.name,
            "log_dir": str(settings.app.log_dir),
        },
        logger_name="foundation.doctor",
    )
    checks.append(_config_check(settings))
    checks.append(_required_directories_check(settings))
    checks.append(_provider_readiness_check(settings))
    checks.append(_secret_lookup_check(settings, environment=environment))
    checks.append(_database_health_check(settings))
    checks.append(_log_path_check(settings))
    checks.append(_events_log_check(settings))
    checks.append(_capability_registry_check(settings, environment=environment))
    return DoctorReport(checks=checks)
