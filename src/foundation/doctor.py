"""Doctor checks for Foundation CLI environment and config readiness."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from foundation.observability import emit_event
from foundation.services import LocalToolService, ToolAvailabilityStatus
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
    if provider_name not in {"openai", "ollama"}:
        return DoctorCheck(
            name="Provider readiness",
            status=DoctorStatus.FAIL,
            summary=f"Provider {settings.provider.name!r} is not supported.",
            detail="Supported providers: openai, ollama.",
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


def _secret_lookup_check(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorCheck:
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


def _external_tools_check(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorCheck:
    service = LocalToolService(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=min(settings.shell.default_timeout_seconds, 30),
        capture_limit_kb=settings.shell.capture_limit_kb,
        environment=environment,
    )
    availability = service.availability_report()

    missing_required = [
        item
        for item in availability
        if item.required and item.status is not ToolAvailabilityStatus.AVAILABLE
    ]
    missing_optional = [
        item
        for item in availability
        if not item.required and item.status is not ToolAvailabilityStatus.AVAILABLE
    ]

    detail_lines = []
    for item in availability:
        state = "available" if item.status is ToolAvailabilityStatus.AVAILABLE else "missing"
        resolved = f" ({item.resolved_command}: {item.path})" if item.path is not None else ""
        line = f"{item.name}: {state}{resolved}"
        if item.status is not ToolAvailabilityStatus.AVAILABLE and item.install_hint:
            line = f"{line} | {item.install_hint}"
        detail_lines.append(line)

    if missing_required:
        return DoctorCheck(
            name="External tools",
            status=DoctorStatus.FAIL,
            summary="Required Stage 4 tool binaries are missing.",
            detail="\n".join(detail_lines),
        )

    if missing_optional:
        return DoctorCheck(
            name="External tools",
            status=DoctorStatus.WARN,
            summary="Some optional Stage 4 tool binaries are missing.",
            detail="\n".join(detail_lines),
        )

    return DoctorCheck(
        name="External tools",
        status=DoctorStatus.PASS,
        summary="All configured Stage 4 tool binaries are available.",
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
    checks.append(_external_tools_check(settings, environment=environment))
    return DoctorReport(checks=checks)
