"""Tests for Slice 3: foundation uninstall + alias-removal + purge."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from foundation.cli import app
from foundation.installer import (
    purge_state_dirs,
    remove_alias_block,
)
from foundation.settings import load_settings
from foundation.shell_alias import MARKER_END, MARKER_START, ShellKind, render_alias_block

# --- remove_alias_block --------------------------------------------------


def test_remove_alias_block_strips_marker_fenced_section(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    block = render_alias_block("fcli", "foundation", ShellKind.ZSH)
    rc.write_text("export PATH=/usr/local/bin:$PATH\n" + block + "# trailing user line\n")

    result = remove_alias_block(rc)
    assert result.removed is True
    assert result.backup_path is not None and result.backup_path.exists()

    body = rc.read_text()
    assert MARKER_START not in body
    assert MARKER_END not in body
    assert "alias fcli=" not in body
    assert "PATH=/usr/local/bin" in body  # untouched
    assert "trailing user line" in body  # untouched


def test_remove_alias_block_idempotent_when_absent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("export PATH=/usr/local/bin:$PATH\n")

    result = remove_alias_block(rc)
    assert result.removed is False
    assert result.backup_path is None
    assert "PATH=/usr/local/bin" in rc.read_text()


def test_remove_alias_block_missing_file_is_noop(tmp_path: Path) -> None:
    rc = tmp_path / "never_existed"
    result = remove_alias_block(rc)
    assert result.removed is False
    assert result.backup_path is None


# --- purge_state_dirs ----------------------------------------------------


def test_purge_state_dirs_removes_three_platform_dirs(tmp_path: Path, monkeypatch) -> None:
    # Build a real AppSettings rooted in tmp so we can assert removal safely.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[app]\n"
        f'workspace_root = "{tmp_path}"\n'
        f'data_dir = "{tmp_path / "data"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        f'log_dir = "{tmp_path / "state" / "logs"}"\n'
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "logs").mkdir()
    (tmp_path / "data" / "marker").write_text("data")
    (tmp_path / "state" / "marker").write_text("state")

    settings = load_settings(config_path)
    result = purge_state_dirs(settings)

    removed = {p for p in result.removed}
    assert (tmp_path / "data").resolve() in removed
    assert (tmp_path / "state").resolve() in removed
    assert config_dir.resolve() in removed


# --- CLI: foundation uninstall -------------------------------------------


def test_cli_uninstall_removes_alias_and_keeps_state_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[app]\n"
        f'workspace_root = "{tmp_path}"\n'
        f'data_dir = "{tmp_path / "data"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()

    rc = tmp_path / ".zshrc"
    rc.write_text(
        "export PATH=/usr/local/bin:$PATH\n"
        + render_alias_block("fcli", "foundation", ShellKind.ZSH)
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "uninstall",
            "--non-interactive",
        ],
    )
    assert result.exit_code == 0, result.output
    assert MARKER_START not in rc.read_text()
    # State dirs preserved when --purge omitted.
    assert (tmp_path / "data").exists()
    assert (tmp_path / "state").exists()


def test_cli_uninstall_purge_requires_yes_in_non_interactive(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[app]\n"
        f'workspace_root = "{tmp_path}"\n'
        f'data_dir = "{tmp_path / "data"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "uninstall",
            "--non-interactive",
            "--purge",
            # no --yes
        ],
    )
    assert result.exit_code == 0
    assert "requires --yes" in result.output.lower()
    # State dirs preserved because the safety check fired.
    assert (tmp_path / "data").exists()
    assert (tmp_path / "state").exists()


def test_cli_uninstall_purge_with_yes_wipes_state(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(
        "[app]\n"
        f'workspace_root = "{tmp_path}"\n'
        f'data_dir = "{tmp_path / "data"}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "uninstall",
            "--non-interactive",
            "--purge",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "state").exists()
    assert not config_dir.exists()
