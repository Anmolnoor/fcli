"""Tests for the `foundation init` setup wizard (Slice A)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundation.cli import app
from foundation.settings import load_settings
from foundation.setup_wizard import (
    WizardChoices,
    apply_choices,
    default_choices,
    render_config_toml,
    write_config_toml,
    write_env_file,
)

# --- pure-helper coverage ------------------------------------------------


def _make_choices(tmp_path: Path, *, provider: str = "openai") -> WizardChoices:
    return WizardChoices(
        provider=provider,
        model="gpt-5-mini" if provider == "openai" else "qwen3:8b",
        workspace_root=tmp_path / "workspace",
        config_path=tmp_path / "config" / "config.toml",
        env_file_path=tmp_path / "config" / "foundation.env",
        api_key_env_var="OPENAI_API_KEY" if provider == "openai" else "OLLAMA_API_KEY",
        api_key="sk-test-value",
    )


def test_render_config_toml_round_trips_through_load_settings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    choices = _make_choices(tmp_path)
    choices.workspace_root = workspace

    write_config_toml(choices.config_path, render_config_toml(choices), overwrite=False)

    settings = load_settings(choices.config_path)
    assert settings.provider.normalized_name() == "openai"
    assert settings.provider.model == "gpt-5-mini"
    assert settings.app.workspace_root.resolve() == workspace.resolve()


def test_write_config_toml_backs_up_existing_file(tmp_path: Path) -> None:
    choices = _make_choices(tmp_path)
    choices.config_path.parent.mkdir(parents=True)
    choices.config_path.write_text('[provider]\nname = "openai"\nmodel = "old"\n')

    backup = write_config_toml(
        choices.config_path,
        render_config_toml(choices),
        overwrite=True,
    )
    assert backup is not None
    assert backup.exists()
    assert "old" in backup.read_text()
    assert "gpt-5-mini" in choices.config_path.read_text()


def test_write_config_toml_refuses_overwrite_without_force(tmp_path: Path) -> None:
    choices = _make_choices(tmp_path)
    choices.config_path.parent.mkdir(parents=True)
    choices.config_path.write_text('[provider]\nname = "openai"\n')

    with pytest.raises(FileExistsError):
        write_config_toml(
            choices.config_path,
            render_config_toml(choices),
            overwrite=False,
        )


def test_write_env_file_preserves_other_vars_and_chmods_600(tmp_path: Path) -> None:
    env_path = tmp_path / "foundation.env"
    env_path.write_text("KEEP_ME=yes\nOPENAI_API_KEY=old-value\n")

    write_env_file(env_path, "OPENAI_API_KEY", "new-value")

    body = env_path.read_text()
    assert "KEEP_ME=yes" in body
    assert "OPENAI_API_KEY=new-value" in body
    assert "old-value" not in body

    mode = os.stat(env_path).st_mode & 0o777
    assert mode == 0o600


def test_write_env_file_creates_file_with_600_when_absent(tmp_path: Path) -> None:
    env_path = tmp_path / "subdir" / "foundation.env"
    write_env_file(env_path, "OPENAI_API_KEY", "sk-fresh")
    assert env_path.read_text().strip() == "OPENAI_API_KEY=sk-fresh"
    assert os.stat(env_path).st_mode & 0o777 == 0o600


def test_default_choices_seeds_from_existing_settings(tmp_path: Path) -> None:
    choices = _make_choices(tmp_path, provider="ollama")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    choices.workspace_root = workspace
    write_config_toml(choices.config_path, render_config_toml(choices), overwrite=False)

    settings = load_settings(choices.config_path)
    seeded = default_choices(settings)
    assert seeded.provider == "ollama"
    assert seeded.model == "qwen3:8b"
    assert seeded.workspace_root.resolve() == workspace.resolve()


def test_apply_choices_writes_config_and_env(tmp_path: Path) -> None:
    choices = _make_choices(tmp_path)
    choices.workspace_root.mkdir(parents=True)

    backup, env_written, alias_result = apply_choices(choices, overwrite=False, write_key=True)
    assert backup is None
    assert env_written is True
    assert alias_result is None
    assert choices.config_path.exists()
    assert choices.env_file_path.exists()
    assert "OPENAI_API_KEY=sk-test-value" in choices.env_file_path.read_text()


def test_apply_choices_skips_env_when_no_key(tmp_path: Path) -> None:
    choices = _make_choices(tmp_path)
    choices.workspace_root.mkdir(parents=True)
    choices.api_key = None

    _backup, env_written, alias_result = apply_choices(choices, overwrite=False, write_key=False)
    assert env_written is False
    assert alias_result is None
    assert not choices.env_file_path.exists()


# --- CLI integration -----------------------------------------------------


def test_cli_init_non_interactive_produces_valid_config(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_path = tmp_path / "cfg" / "config.toml"

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
        ],
    )

    assert result.exit_code == 0, result.output
    assert config_path.exists()
    env_path = config_path.parent / "foundation.env"
    assert env_path.exists()
    assert "OPENAI_API_KEY=sk-cli-test" in env_path.read_text()

    settings = load_settings(config_path)
    assert settings.provider.normalized_name() == "openai"
    assert settings.app.workspace_root.resolve() == workspace.resolve()


def test_cli_init_non_interactive_requires_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "cfg" / "config.toml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(config_path), "init", "--non-interactive", "--no-probe"],
    )
    assert result.exit_code != 0


def test_cli_init_non_interactive_refuses_existing_without_force(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_path = tmp_path / "cfg" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('[provider]\nname = "openai"\nmodel = "old-model"\n')

    runner = CliRunner()
    args_base = [
        "--config",
        str(config_path),
        "init",
        "--non-interactive",
        "--provider",
        "openai",
        "--model",
        "gpt-5-mini",
        "--api-key",
        "sk-x",
        "--workspace",
        str(workspace),
        "--no-probe",
    ]

    refused = runner.invoke(app, args_base)
    assert refused.exit_code != 0
    assert "exists" in refused.output.lower()

    forced = runner.invoke(app, [*args_base, "--force"])
    assert forced.exit_code == 0, forced.output
    backup = config_path.with_suffix(config_path.suffix + ".bak")
    assert backup.exists()
    assert "old-model" in backup.read_text()


def test_config_init_alias_runs_same_wizard(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_path = tmp_path / "cfg" / "config.toml"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "config",
            "init",
            "--non-interactive",
            "--provider",
            "ollama",
            "--model",
            "qwen3:8b",
            "--api-key",
            "ignored-for-local-ollama",
            "--workspace",
            str(workspace),
            "--no-probe",
        ],
    )
    assert result.exit_code == 0, result.output

    settings = load_settings(config_path)
    assert settings.provider.normalized_name() == "ollama"
    env_path = config_path.parent / "foundation.env"
    assert "OLLAMA_API_KEY=ignored-for-local-ollama" in env_path.read_text()
