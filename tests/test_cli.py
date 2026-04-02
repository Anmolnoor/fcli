from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest
from keyring.errors import NoKeyringError
from typer.testing import CliRunner

from foundation.cli import app
from foundation.models import (
    AssistantMessage,
    AssistantPlan,
    ContextSnapshot,
    OrchestrationResult,
    OrchestrationSummary,
    ProviderResponseMetadata,
    UserRequest,
)

runner = CliRunner()


def _write_stage_2_config(tmp_path: Path) -> Path:
    workspace_root = tmp_path / "workspace"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    history_dir = tmp_path / "history"
    for path in (workspace_root, data_dir, state_dir, log_dir, history_dir):
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
                "[history]",
                f'database_path = "{history_dir / "history.sqlite3"}"',
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _tool_env(tmp_path: Path, scripts: dict[str, str]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, script in scripts.items():
        _write_executable(bin_dir / name, script)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


def _chat_result(message: str) -> OrchestrationResult:
    return OrchestrationResult(
        request=UserRequest(message=message),
        context=ContextSnapshot(
            workspace_root="/tmp/workspace",
            request_cwd="/tmp/workspace",
            approval_mode="prompt",
            available_tools=["git", "rg"],
        ),
        plan=AssistantPlan(
            assistant_message="I will inspect the repository state.",
            actions=[],
        ),
        planning_metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
        policy_decisions=[],
        execution_results=[],
        assistant_message=AssistantMessage(content="I will inspect the repository state."),
        summary=OrchestrationSummary(
            executed_actions=0,
            pending_approval_actions=0,
            blocked_actions=0,
            failed_actions=0,
            skipped_actions=0,
            text="No actions were needed for this request.",
        ),
    )


def test_cli_help_displays_core_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Foundation CLI" in result.stdout
    assert "run" in result.stdout
    assert "chat" in result.stdout
    assert "config" in result.stdout
    assert "tools" in result.stdout
    assert "history" in result.stdout
    assert "doctor" in result.stdout


def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "foundation 0.1.0" in result.stdout


def test_config_show_redacts_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "config"],
        env={"OPENAI_API_KEY": "cli-secret-value"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert "cli-secret-value" not in result.stdout
    assert payload["settings"]["provider"]["api_key"] == "[redacted]"
    assert payload["metadata"]["config_path"] == str(config_path.resolve())


def test_config_validate_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(app, ["--config", str(config_path), "config", "validate"])

    assert result.exit_code == 0
    assert "Configuration is valid." in result.stdout
    assert "Provider credential sources" in result.stdout


def test_doctor_reports_healthy_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "doctor"],
        env={"OPENAI_API_KEY": "doctor-secret"},
    )

    assert result.exit_code == 0
    assert "PASS" in result.stdout
    assert "Secret lookup health" in result.stdout
    assert "Resolved provider credentials from $OPENAI_API_KEY." in result.stdout


def test_doctor_fails_when_credentials_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)

    def _raise_no_keyring(*_args: object) -> str | None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr("foundation.settings.keyring.get_password", _raise_no_keyring)

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "Secret lookup health" in result.stdout


def test_run_executes_a_buffered_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "run",
            "--mode",
            "buffered",
            "--",
            sys.executable,
            "-c",
            "print('cli-stage-3')",
        ],
    )

    assert result.exit_code == 0
    assert "cli-stage-3" in result.stdout
    assert "Execution Summary" in result.stdout


def test_run_rejects_out_of_workspace_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "run",
            "--cwd",
            str(outside_dir),
            "--mode",
            "buffered",
            "--",
            sys.executable,
            "-c",
            "print('nope')",
        ],
    )

    assert result.exit_code == 2
    assert "workspace root" in result.stdout


def test_tools_search_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    env = _tool_env(
        tmp_path,
        {
            "rg": """
                import json

                payload = {
                    "type": "match",
                    "data": {
                        "path": {"text": "src/example.py"},
                        "lines": {"text": "print('stage4')\\n"},
                        "line_number": 3,
                        "submatches": [{"start": 7, "end": 13}],
                    },
                }
                print(json.dumps(payload))
            """,
        },
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "tools", "search", "stage4", "--json"],
        env=env,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["matches"][0]["path"] == "src/example.py"
    assert payload["matches"][0]["line_number"] == 3


def test_chat_emits_json_for_one_shot_orchestration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result(request.message)

    monkeypatch.setattr("foundation.cli._build_orchestrator", lambda _settings: StubOrchestrator())

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "chat",
            "--json",
            "summarize",
            "git",
            "status",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["request"]["message"] == "summarize git status"
    assert payload["assistant_message"]["content"] == "I will inspect the repository state."


def test_history_lists_audited_run_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    run_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "run",
            "--mode",
            "buffered",
            "--",
            sys.executable,
            "-c",
            "print('history-run')",
        ],
    )
    assert run_result.exit_code == 0

    history_result = runner.invoke(
        app,
        ["--config", str(config_path), "history", "--json"],
    )

    assert history_result.exit_code == 0
    payload = json.loads(history_result.stdout)
    assert payload[0]["kind"] == "run"
    assert "python" in payload[0]["command_preview"]


def test_history_can_render_session_detail_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    run_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "run",
            "--mode",
            "buffered",
            "--",
            sys.executable,
            "-c",
            "print('detail-run')",
        ],
    )
    assert run_result.exit_code == 0

    history_result = runner.invoke(
        app,
        ["--config", str(config_path), "history", "--json"],
    )
    session_id = json.loads(history_result.stdout)[0]["session_id"]

    detail_result = runner.invoke(
        app,
        ["--config", str(config_path), "history", "--session", session_id, "--json"],
    )

    assert detail_result.exit_code == 0
    payload = json.loads(detail_result.stdout)
    assert payload["session_id"] == session_id
    assert payload["commands"][0]["source"] == "cli.run"
