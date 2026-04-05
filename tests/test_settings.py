from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundation.settings import (
    ApprovalMode,
    LogLevel,
    SettingsLoadError,
    default_env_file_path,
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


def test_load_settings_honors_provider_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'name = "openai"',
                'model = "gpt-5-mini"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOUNDATION_PROVIDER__MODEL", "gpt-5-nano")

    settings = load_settings(
        config_path=config_path,
        overrides={
            "provider": {
                "model": "gpt-5",
                "base_url": "https://example.test/v1",
                "request_timeout_seconds": 90,
            }
        },
    )

    assert settings.provider.model == "gpt-5"
    assert settings.provider.effective_base_url() == "https://example.test/v1"
    assert settings.provider.request_timeout_seconds == 90
    assert settings.cli_overrides == {
        "provider.model": "gpt-5",
        "provider.base_url": "https://example.test/v1",
        "provider.request_timeout_seconds": 90,
    }


def test_load_settings_reads_paired_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'name = "ollama"',
                'model = "from-config"',
                'base_url = "https://ollama.com/api"',
            ]
        ),
        encoding="utf-8",
    )
    env_file_path = default_env_file_path(config_path)
    env_file_path.write_text(
        "\n".join(
            [
                "FOUNDATION_PROVIDER__MODEL=from-env-file",
                "OLLAMA_API_KEY=env-file-secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("FOUNDATION_PROVIDER__MODEL", raising=False)

    settings = load_settings(config_path=config_path)

    assert settings.provider.model == "from-env-file"
    assert settings.env_file_path == env_file_path
    assert settings.env_file_exists is True
    resolution = settings.provider.resolve_api_key(
        environment=settings.provider_environment(),
    )
    assert resolution.status.value == "resolved"


def test_process_environment_overrides_paired_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'name = "ollama"',
                'model = "from-config"',
            ]
        ),
        encoding="utf-8",
    )
    env_file_path = default_env_file_path(config_path)
    env_file_path.write_text(
        "FOUNDATION_PROVIDER__MODEL=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FOUNDATION_PROVIDER__MODEL", "from-process-env")

    settings = load_settings(config_path=config_path)

    assert settings.provider.model == "from-process-env"


def test_ollama_provider_uses_provider_specific_defaults(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'name = "ollama"',
                'model = "gpt-oss:20b"',
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path)

    assert settings.provider.effective_base_url() == "http://localhost:11434/api"
    assert settings.provider.credential_source_order() == [
        "keychain:foundation/ollama_api_key",
        "env:OLLAMA_API_KEY",
    ]
    assert settings.provider.credentials_required() is False


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
    assert "resolved_base_url" in rendered
    assert "env_file_path" in rendered
