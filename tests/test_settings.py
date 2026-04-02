from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundation.settings import (
    ApprovalMode,
    LogLevel,
    SettingsLoadError,
    load_settings,
    render_settings_payload,
)


def test_load_settings_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    settings = load_settings(config_path=tmp_path / "missing.toml")

    assert settings.app_name == "foundation"
    assert settings.debug is False
    assert settings.logging.level is LogLevel.WARNING
    assert settings.config_exists is False
    assert settings.config_path == (tmp_path / "missing.toml").resolve()
    assert settings.workspace_root == tmp_path.resolve()


def test_load_settings_honors_toml_env_and_cli_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    file_workspace = tmp_path / "workspace-from-file"
    env_workspace = tmp_path / "workspace-from-env"
    cli_workspace = tmp_path / "workspace-from-cli"
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                f'workspace_root = "{file_workspace}"',
                "",
                "[logging]",
                'level = "WARNING"',
                "",
                "[approval]",
                'mode = "manual"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOUNDATION_LOGGING__LEVEL", "ERROR")
    monkeypatch.setenv("FOUNDATION_APP__WORKSPACE_ROOT", str(env_workspace))

    settings = load_settings(
        config_path=config_path,
        overrides={
            "app": {"workspace_root": cli_workspace},
            "approval": {"mode": ApprovalMode.AUTO},
        },
    )

    assert settings.workspace_root == cli_workspace.resolve()
    assert settings.logging.level is LogLevel.ERROR
    assert settings.approval.mode is ApprovalMode.AUTO
    assert settings.cli_overrides == {
        "app.workspace_root": str(cli_workspace),
        "approval.mode": "auto",
    }


def test_load_settings_rejects_invalid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.toml"
    config_path.write_text('[app\nworkspace_root = "/tmp"\n', encoding="utf-8")

    with pytest.raises(SettingsLoadError):
        load_settings(config_path=config_path)


def test_render_settings_payload_redacts_provider_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    settings = load_settings(config_path=tmp_path / "missing.toml")
    payload = render_settings_payload(settings)
    rendered = json.dumps(payload)

    assert "super-secret-value" not in rendered
    assert "[redacted]" in rendered
    assert "OPENAI_API_KEY" in rendered
