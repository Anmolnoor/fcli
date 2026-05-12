"""Tests for the shell-alias installer used by `foundation init`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from foundation.cli import app
from foundation.shell_alias import (
    MARKER_END,
    MARKER_START,
    ShellKind,
    detect_shell,
    install_alias,
    render_alias_block,
)

# --- detect_shell --------------------------------------------------------


def test_detect_shell_reads_shell_env_zsh(tmp_path: Path) -> None:
    (tmp_path / ".zshrc").write_text("")
    detected = detect_shell(shell_env="/bin/zsh", home=tmp_path)
    assert detected is not None
    assert detected.kind is ShellKind.ZSH
    assert detected.rc_path == tmp_path / ".zshrc"


def test_detect_shell_reads_shell_env_bash_prefers_bashrc(tmp_path: Path) -> None:
    (tmp_path / ".bashrc").write_text("")
    (tmp_path / ".bash_profile").write_text("")
    detected = detect_shell(shell_env="/usr/local/bin/bash", home=tmp_path)
    assert detected is not None
    assert detected.kind is ShellKind.BASH
    assert detected.rc_path == tmp_path / ".bashrc"


def test_detect_shell_returns_canonical_rc_when_none_exists(tmp_path: Path) -> None:
    detected = detect_shell(shell_env="/usr/bin/fish", home=tmp_path)
    assert detected is not None
    assert detected.kind is ShellKind.FISH
    assert detected.rc_path == tmp_path / ".config" / "fish" / "config.fish"


def test_detect_shell_falls_back_to_filesystem_when_env_missing(tmp_path: Path) -> None:
    (tmp_path / ".bashrc").write_text("")
    detected = detect_shell(shell_env="", home=tmp_path)
    assert detected is not None
    assert detected.kind is ShellKind.BASH


def test_detect_shell_returns_none_when_unknown_shell_and_no_rc(tmp_path: Path) -> None:
    detected = detect_shell(shell_env="/opt/exotic/myshell", home=tmp_path)
    assert detected is None


# --- render_alias_block --------------------------------------------------


def test_render_alias_block_bash_zsh_uses_double_quotes() -> None:
    block = render_alias_block("fcli", "foundation", ShellKind.ZSH)
    assert MARKER_START in block
    assert MARKER_END in block
    assert 'alias fcli="foundation"' in block


def test_render_alias_block_fish_uses_single_quoted_form() -> None:
    block = render_alias_block("fcli", "foundation", ShellKind.FISH)
    assert "alias fcli 'foundation'" in block


def test_render_alias_block_escapes_double_quotes_for_bash() -> None:
    block = render_alias_block("fcli", 'foundation "x"', ShellKind.BASH)
    assert 'alias fcli="foundation \\"x\\""' in block


# --- install_alias -------------------------------------------------------


def test_install_alias_appends_block_when_absent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("export PATH=/usr/local/bin:$PATH\n")
    block = render_alias_block("fcli", "foundation", ShellKind.ZSH)

    result = install_alias(rc, block)
    assert result.installed is True
    assert result.replaced is False
    assert result.backup_path is None

    body = rc.read_text()
    assert "PATH=/usr/local/bin" in body  # untouched
    assert 'alias fcli="foundation"' in body
    assert body.count(MARKER_START) == 1


def test_install_alias_replaces_existing_block_in_place(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "export PATH=/usr/local/bin:$PATH\n"
        f"{MARKER_START}\n"
        'alias fcli="old-target"\n'
        f"{MARKER_END}\n"
        "# trailing user line\n"
    )
    block = render_alias_block("fcli", "foundation", ShellKind.ZSH)

    result = install_alias(rc, block)
    assert result.installed is True
    assert result.replaced is True
    assert result.backup_path is not None and result.backup_path.exists()
    assert "old-target" in result.backup_path.read_text()

    body = rc.read_text()
    assert "old-target" not in body
    assert 'alias fcli="foundation"' in body
    assert body.count(MARKER_START) == 1
    assert "trailing user line" in body  # untouched
    assert "PATH=/usr/local/bin" in body  # untouched


def test_install_alias_creates_file_when_absent(tmp_path: Path) -> None:
    rc = tmp_path / "subdir" / ".zshrc"
    block = render_alias_block("fcli", "foundation", ShellKind.ZSH)

    result = install_alias(rc, block)
    assert result.installed is True
    assert result.replaced is False
    assert rc.exists()
    assert 'alias fcli="foundation"' in rc.read_text()


# --- CLI integration -----------------------------------------------------


def test_cli_init_installs_alias_when_flag_passed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_path = tmp_path / "cfg" / "config.toml"
    rc_path = tmp_path / ".zshrc"
    rc_path.write_text("export FOO=1\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "init",
            "--non-interactive",
            "--provider",
            "openai",
            "--model",
            "gpt-5-mini",
            "--api-key",
            "sk-cli-test",
            "--workspace",
            str(workspace),
            "--no-probe",
            "--alias",
            "--shell-rc",
            str(rc_path),
        ],
    )

    assert result.exit_code == 0, result.output
    body = rc_path.read_text()
    assert 'alias fcli="foundation"' in body
    assert "FOO=1" in body


def test_cli_init_alias_default_off_in_non_interactive(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_path = tmp_path / "cfg" / "config.toml"
    rc_path = tmp_path / ".zshrc"
    rc_path.write_text("export FOO=1\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "init",
            "--non-interactive",
            "--provider",
            "openai",
            "--model",
            "gpt-5-mini",
            "--api-key",
            "sk-cli-test",
            "--workspace",
            str(workspace),
            "--no-probe",
            "--shell-rc",
            str(rc_path),
        ],
    )

    assert result.exit_code == 0, result.output
    body = rc_path.read_text()
    assert "alias fcli" not in body  # opt-in flag was not passed
