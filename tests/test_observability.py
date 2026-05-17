from __future__ import annotations

import json
import logging
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from foundation.doctor import DoctorCheck, DoctorStatus, run_doctor
from foundation.logging import configure_logging
from foundation.models import TerminalLogRouting
from foundation.observability import (
    EVENT_EXCEPTION,
    EVENT_PROVIDER_CALL_STARTED,
    EVENT_SESSION_START,
    STRUCTURED_LOG_SCHEMA_VERSION,
    emit_event,
    emit_exception,
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_doctor_config(tmp_path: Path, *, log_dir: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    for path in (workspace_root, data_dir, state_dir):
        path.mkdir(parents=True, exist_ok=True)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                f'workspace_root = "{workspace_root}"',
                f'data_dir = "{data_dir}"',
                f'state_dir = "{state_dir}"',
                f'log_dir = "{log_dir}"',
                "",
                "[provider]",
                'name = "openai"',
                'model = "gpt-5-mini"',
                'api_key_env_var = "OPENAI_API_KEY"',
                "",
                "[history]",
                f'database_path = "{state_dir / "history.sqlite3"}"',
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _stub_capability_registry_check(
    _settings: object,
    *,
    environment: dict[str, str] | None = None,
) -> DoctorCheck:
    del environment
    return DoctorCheck(
        name="Capability registry",
        status=DoctorStatus.PASS,
        summary="Capability registry checks are stubbed in this unit test.",
    )


@pytest.fixture
def preserve_root_logger() -> Iterator[None]:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_propagate = root_logger.propagate

    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            handler.close()
        root_logger.handlers.clear()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        root_logger.propagate = original_propagate


def test_emit_event_redacts_sensitive_payload_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="foundation.events")

    emit_event(
        EVENT_PROVIDER_CALL_STARTED,
        payload={
            "api_key": "super-secret-value",
            "nested": {
                "token": "nested-secret",
                "safe": "visible",
            },
            "cwd": Path("/tmp/workspace"),
        },
    )

    record = next(
        captured
        for captured in caplog.records
        if getattr(captured, "foundation_event", None) == EVENT_PROVIDER_CALL_STARTED
    )

    assert getattr(record, "foundation_event_schema_version", None) == STRUCTURED_LOG_SCHEMA_VERSION
    assert getattr(record, "foundation_event_payload", None) == {
        "api_key": "[redacted]",
        "nested": {
            "token": "[redacted]",
            "safe": "visible",
        },
        "cwd": "/tmp/workspace",
    }


def test_emit_exception_includes_traceback_when_requested(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="foundation.events")

    try:
        raise ValueError("boom")
    except ValueError as exc:
        emit_exception(
            EVENT_EXCEPTION,
            exc,
            payload={"password": "should-not-leak"},
            include_trace=True,
        )

    record = next(
        captured
        for captured in caplog.records
        if getattr(captured, "foundation_event", None) == EVENT_EXCEPTION
    )

    payload = getattr(record, "foundation_event_payload", {})
    assert payload["error_type"] == "ValueError"
    assert payload["error"] == "boom"
    assert payload["password"] == "[redacted]"
    assert "ValueError: boom" in payload["traceback"]


def test_configure_logging_writes_structured_json_events(
    tmp_path: Path,
    preserve_root_logger: Iterator[None],
) -> None:
    del preserve_root_logger
    log_path = tmp_path / "foundation.log"

    configure_logging(level="INFO", structured=True, log_path=log_path)
    emit_event(
        EVENT_SESSION_START,
        payload={"session_id": "session-123"},
    )

    for handler in logging.getLogger().handlers:
        handler.flush()

    entries = log_path.read_text(encoding="utf-8").splitlines()
    assert len(entries) == 1

    payload = json.loads(entries[0])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "foundation.events"
    assert payload["message"] == f"event_name={EVENT_SESSION_START}"
    assert payload["event"] == EVENT_SESSION_START
    assert payload["event_schema_version"] == STRUCTURED_LOG_SCHEMA_VERSION
    assert payload["event_payload"] == {"session_id": "session-123"}


def test_configure_logging_file_only_omits_terminal_stream_handler(
    tmp_path: Path,
    preserve_root_logger: Iterator[None],
) -> None:
    del preserve_root_logger
    log_path = tmp_path / "foundation.log"

    configure_logging(
        level="INFO",
        structured=False,
        log_path=log_path,
        routing=TerminalLogRouting.FILE_ONLY,
    )

    handlers = logging.getLogger().handlers
    assert any(isinstance(handler, logging.FileHandler) for handler in handlers)
    assert not any(type(handler) is logging.StreamHandler for handler in handlers)


def test_run_doctor_warns_when_log_dir_is_missing_but_creatable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        log_dir=tmp_path / "state" / "logs",
    )
    monkeypatch.setattr(
        "foundation.doctor._capability_registry_check",
        _stub_capability_registry_check,
    )

    report = run_doctor(
        config_path=config_path,
        environment={"OPENAI_API_KEY": "doctor-secret"},
    )
    checks = {check.name: check for check in report.checks}

    assert report.exit_code == 0
    assert checks["Log path"].status is DoctorStatus.WARN
    assert checks["Log path"].summary == "Log directory is missing but creatable."
    assert checks["Secret lookup health"].status is DoctorStatus.PASS
    assert checks["History database"].status is DoctorStatus.PASS


def test_run_doctor_fails_when_log_dir_is_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_log_path = tmp_path / "logs-file"
    blocked_log_path.write_text("not a directory\n", encoding="utf-8")
    config_path = _write_doctor_config(tmp_path, log_dir=blocked_log_path)
    monkeypatch.setattr(
        "foundation.doctor._capability_registry_check",
        _stub_capability_registry_check,
    )

    report = run_doctor(
        config_path=config_path,
        environment={"OPENAI_API_KEY": "doctor-secret"},
    )
    checks = {check.name: check for check in report.checks}

    assert report.exit_code == 1
    assert checks["Required directories"].status is DoctorStatus.FAIL
    assert checks["Log path"].status is DoctorStatus.FAIL
    assert (
        checks["Log path"].detail == f"{blocked_log_path.resolve()} exists but is not a directory."
    )


def test_run_doctor_warns_when_capability_store_is_missing_but_creatable(
    tmp_path: Path,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        log_dir=tmp_path / "state" / "logs",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("rg", "git", "fd", "man", "tldr"):
        _write_executable(bin_dir / name, "print('')\n")

    report = run_doctor(
        config_path=config_path,
        environment={
            "OPENAI_API_KEY": "doctor-secret",
            "PATH": str(bin_dir),
        },
    )
    checks = {check.name: check for check in report.checks}

    assert report.exit_code == 0
    assert checks["Capability registry"].status is DoctorStatus.WARN
    assert checks["Capability registry"].summary == "Capability store is missing but creatable."
    assert checks["Capability registry"].detail is not None
    assert "Capability store root:" in checks["Capability registry"].detail
    assert not (tmp_path / "data" / "capabilities").exists()
