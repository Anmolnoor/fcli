"""Tests for the v4 Stage 02 doctor surface."""

from __future__ import annotations

from pathlib import Path

from foundation.doctor import DoctorStatus, _events_log_check
from foundation.settings import (
    AppSection,
    AppSettings,
    MonitorRetentionSection,
    MonitorSection,
)


def _build_settings(
    *,
    events_dir: Path,
    enabled: bool = True,
    transports: list[str] | None = None,
    socket_path: Path | None = None,
    http_port: int | None = None,
) -> AppSettings:
    settings = AppSettings(
        app=AppSection(workspace_root=events_dir.parent),
        monitor=MonitorSection(
            enabled=enabled,
            events_dir=events_dir,
            retention=MonitorRetentionSection(max_sessions=10, max_bytes=1024 * 1024),
            live_transports=transports or [],
            socket_path=socket_path,
            http_port=http_port,
        ),
    )
    return settings


def test_events_log_check_reports_pass_when_dir_writable(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    check = _events_log_check(_build_settings(events_dir=events_dir))
    assert check.status is DoctorStatus.PASS
    assert "events_dir" in check.detail
    assert "retention: max_sessions=10" in check.detail
    assert "current usage" in check.detail


def test_events_log_check_reports_session_count_and_bytes(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / "sess-a.ndjson").write_text("a\n", encoding="utf-8")
    (events_dir / "sess-b.ndjson").write_text("bb\n", encoding="utf-8")
    check = _events_log_check(_build_settings(events_dir=events_dir))
    assert check.status is DoctorStatus.PASS
    assert "sessions=2" in check.detail
    # 2 + 3 bytes (newline counts).
    assert "bytes=5" in check.detail


def test_events_log_check_warns_when_dir_missing_but_creatable(
    tmp_path: Path,
) -> None:
    events_dir = tmp_path / "missing-events"
    check = _events_log_check(_build_settings(events_dir=events_dir))
    assert check.status is DoctorStatus.WARN
    assert "missing but creatable" in check.summary


def test_events_log_check_warns_when_disabled(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    check = _events_log_check(_build_settings(events_dir=events_dir, enabled=False))
    assert check.status is DoctorStatus.WARN
    assert "disabled" in check.summary.lower()


def test_events_log_check_lists_no_live_transports_by_default(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    check = _events_log_check(_build_settings(events_dir=events_dir))
    assert "live transports: none" in check.detail


def test_events_log_check_lists_configured_unix_transport(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    socket_path = tmp_path / "fcli.sock"
    check = _events_log_check(
        _build_settings(
            events_dir=events_dir,
            transports=["unix"],
            socket_path=socket_path,
        )
    )
    assert "live transports configured: unix" in check.detail
    assert f"socket_path: {socket_path}" in check.detail


def test_events_log_check_lists_configured_http_transport(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    check = _events_log_check(
        _build_settings(
            events_dir=events_dir,
            transports=["http"],
            http_port=8765,
        )
    )
    assert "live transports configured: http" in check.detail
    assert "http_port: 8765" in check.detail


def test_events_log_check_fails_when_path_is_a_file(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.write_text("not a dir", encoding="utf-8")
    check = _events_log_check(_build_settings(events_dir=events_dir))
    assert check.status is DoctorStatus.FAIL
    assert "not a directory" in check.summary
