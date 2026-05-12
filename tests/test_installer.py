"""Tests for installer.py (Slice 2): detection + update/uninstall planning."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foundation.installer import (
    InstallMechanism,
    build_uninstall_plan,
    build_update_plan,
    detect_install_mechanism,
    fetch_latest_sha,
)

# --- detect_install_mechanism --------------------------------------------


def test_detect_pipx_install(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    pipx_venv = home / ".local" / "pipx" / "venvs" / "foundation-cli"
    pipx_venv.mkdir(parents=True)
    exe = pipx_venv / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("")

    monkeypatch.setattr(Path, "home", lambda: home)

    probe = detect_install_mechanism(executable=exe)
    assert probe.mechanism is InstallMechanism.PIPX
    assert "pipx" in probe.detail


def test_detect_dev_checkout(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    exe = repo / ".venv" / "bin" / "python"
    exe.write_text("")
    (repo / "pyproject.toml").write_text('name = "foundation-cli"\nversion = "0.2.0"\n')

    monkeypatch.setattr(Path, "home", lambda: home)

    probe = detect_install_mechanism(executable=exe)
    assert probe.mechanism is InstallMechanism.DEV_CHECKOUT
    assert probe.install_root == repo.resolve()


def test_detect_pip_user_install(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    site_bin = home / ".local" / "bin"
    site_bin.mkdir(parents=True)
    exe = site_bin / "python3.12"
    exe.write_text("")

    monkeypatch.setattr(Path, "home", lambda: home)

    probe = detect_install_mechanism(executable=exe)
    assert probe.mechanism is InstallMechanism.PIP_USER


def test_detect_unknown_install_far_from_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "opt" / "weird" / "python"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")

    monkeypatch.setattr(Path, "home", lambda: home)

    probe = detect_install_mechanism(executable=elsewhere)
    assert probe.mechanism is InstallMechanism.UNKNOWN


# --- build_update_plan ---------------------------------------------------


def test_update_plan_for_pipx(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.PIPX, tmp_path)
    plan = build_update_plan(probe, ref="main")
    assert plan.command is not None
    assert plan.command[:3] == ["pipx", "install", "--force"]
    assert "@main" in plan.command[-1]


def test_update_plan_for_pip_user(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.PIP_USER, tmp_path)
    plan = build_update_plan(probe, ref="main")
    assert plan.command is not None
    assert plan.command[0] == sys.executable
    assert "--upgrade" in plan.command


def test_update_plan_for_dev_checkout_returns_none_command(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.DEV_CHECKOUT, tmp_path)
    plan = build_update_plan(probe)
    assert plan.command is None
    assert "git pull" in plan.detail


def test_update_plan_respects_custom_ref(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.PIPX, tmp_path)
    plan = build_update_plan(probe, ref="v1.0.0")
    assert plan.command is not None
    assert plan.command[-1].endswith("@v1.0.0")


# --- build_uninstall_plan ------------------------------------------------


def test_uninstall_plan_for_pipx(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.PIPX, tmp_path)
    plan = build_uninstall_plan(probe)
    assert plan.command == ["pipx", "uninstall", "foundation-cli"]


def test_uninstall_plan_for_dev_checkout_returns_none(tmp_path: Path) -> None:
    probe = _fake_probe(InstallMechanism.DEV_CHECKOUT, tmp_path)
    plan = build_uninstall_plan(probe)
    assert plan.command is None


# --- fetch_latest_sha ----------------------------------------------------


def test_fetch_latest_sha_parses_response() -> None:
    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    payload = b'{"sha": "abcdef1234567890"}'
    with patch("foundation.installer.urllib.request.urlopen", return_value=_FakeResp(payload)):
        assert fetch_latest_sha() == "abcdef1"


def test_fetch_latest_sha_returns_none_on_network_error() -> None:
    import urllib.error

    with patch(
        "foundation.installer.urllib.request.urlopen",
        side_effect=urllib.error.URLError("boom"),
    ):
        assert fetch_latest_sha() is None


def test_fetch_latest_sha_returns_none_on_bad_json() -> None:
    class _FakeResp:
        def read(self) -> bytes:
            return b"not json"

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch("foundation.installer.urllib.request.urlopen", return_value=_FakeResp()):
        assert fetch_latest_sha() is None


# --- helpers -------------------------------------------------------------


def _fake_probe(mechanism: InstallMechanism, tmp_path: Path) -> Any:
    from foundation.installer import InstallProbe

    return InstallProbe(
        mechanism=mechanism,
        executable=tmp_path / "bin" / "python",
        detail="test",
        install_root=tmp_path,
    )


# --- CLI: foundation update --dry-run ------------------------------------


def test_cli_update_dry_run_prints_command_without_executing(monkeypatch) -> None:
    from typer.testing import CliRunner

    from foundation.cli import app
    from foundation.installer import InstallMechanism, InstallProbe

    fake = InstallProbe(
        mechanism=InstallMechanism.PIPX,
        executable=Path("/tmp/fake-pipx-exe"),
        detail="fake pipx",
        install_root=Path("/tmp/fake"),
    )
    monkeypatch.setattr("foundation.installer.detect_install_mechanism", lambda **_kw: fake)
    monkeypatch.setattr("foundation.installer.fetch_latest_sha", lambda **_kw: "deadbee")

    runner = CliRunner()
    result = runner.invoke(app, ["update", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "pipx install --force" in result.output
    assert "deadbee" in result.output
