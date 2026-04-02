"""Doctor checks for Stage 2 environment and config readiness."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


def _secret_lookup_check(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorCheck:
    resolution = settings.provider.resolve_api_key(environment=environment)

    if resolution.status is SecretResolutionStatus.RESOLVED:
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.PASS,
            summary=resolution.detail,
        )

    if resolution.status is SecretResolutionStatus.UNAVAILABLE:
        return DoctorCheck(
            name="Secret lookup health",
            status=DoctorStatus.FAIL,
            summary="Provider credentials could not be checked through the keychain.",
            detail=resolution.detail,
        )

    return DoctorCheck(
        name="Secret lookup health",
        status=DoctorStatus.FAIL,
        summary="Provider credentials are not configured.",
        detail=resolution.detail,
    )


def run_doctor(
    config_path: Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run the Stage 2 doctor checks."""
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

    checks.append(_config_check(settings))
    checks.append(_required_directories_check(settings))
    checks.append(_secret_lookup_check(settings, environment=environment))
    return DoctorReport(checks=checks)
