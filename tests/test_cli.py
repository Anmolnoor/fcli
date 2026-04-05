from __future__ import annotations

import json
import os
import sys
import textwrap
import types
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from keyring.errors import NoKeyringError
from rich.console import Console
from typer.testing import CliRunner

from foundation.cli import _build_chat_prompt_session, _render_interactive_chat_help, app
from foundation.models import (
    AssistantMessage,
    AssistantPlan,
    ContextSnapshot,
    OrchestrationResult,
    OrchestrationSummary,
    ProviderMessageRole,
    ProviderResponseMetadata,
    SessionStatus,
    UserRequest,
)
from foundation.settings import ApprovalMode, default_env_file_path, load_settings

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


class FakePromptSession:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def prompt(self, message: str) -> str:
        self.prompts.append(message)
        if not self._responses:
            raise EOFError
        return self._responses.pop(0)


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
    assert "Provider: openai" in result.stdout
    assert "Model: gpt-5-mini" in result.stdout
    assert "Base URL: https://api.openai.com/v1" in result.stdout
    assert "Request timeout: 60s" in result.stdout
    assert "Provider credential sources" in result.stdout


def test_config_show_reflects_provider_cli_overrides(
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
            "--model",
            "gpt-5",
            "--base-url",
            "https://example.test/v1",
            "--provider-timeout",
            "90",
            "config",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert payload["settings"]["provider"]["model"] == "gpt-5"
    assert payload["settings"]["provider"]["resolved_base_url"] == "https://example.test/v1"
    assert payload["settings"]["provider"]["request_timeout_seconds"] == 90
    assert payload["metadata"]["cli_overrides"] == {
        "provider.model": "gpt-5",
        "provider.base_url": "https://example.test/v1",
        "provider.request_timeout_seconds": 90,
    }


def test_config_show_includes_paired_env_file_metadata(tmp_path: Path) -> None:
    config_path = _write_stage_2_config(tmp_path)
    env_file_path = default_env_file_path(config_path)
    env_file_path.write_text(
        "FOUNDATION_PROVIDER__MODEL=env-file-model\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--config", str(config_path), "config"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["metadata"]["env_file_path"] == str(env_file_path)
    assert payload["metadata"]["env_file_exists"] is True
    assert payload["settings"]["provider"]["model"] == "env-file-model"


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
    assert "Model: gpt-5-mini" in result.stdout
    assert "Base URL: https://api.openai.com/v1" in result.stdout


def test_doctor_respects_provider_cli_overrides(
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
            "--model",
            "gpt-5",
            "--base-url",
            "https://example.test/v1",
            "--provider-timeout",
            "90",
            "doctor",
        ],
        env={"OPENAI_API_KEY": "doctor-secret"},
    )

    assert result.exit_code == 0
    assert "Model: gpt-5" in result.stdout
    assert "Base URL: https://example.test/v1" in result.stdout
    assert "Request timeout: 90s" in result.stdout


def test_doctor_allows_local_ollama_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[provider]\nname = "ollama"\nmodel = "gpt-oss:20b"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "Provider: ollama" in result.stdout
    assert "Base URL: http://localhost:11434/api" in result.stdout
    assert "Credentials required: no" in result.stdout
    assert "Provider credentials are optional" in result.stdout


def test_doctor_reads_ollama_cloud_credentials_from_paired_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + (
            "\n[provider]\n"
            'name = "ollama"\n'
            'model = "qwen3.5:397b-cloud"\n'
            'base_url = "https://ollama.com/api"\n'
            'api_key_env_var = "OLLAMA_API_KEY"\n'
        ),
        encoding="utf-8",
    )
    env_file_path = default_env_file_path(config_path)
    env_file_path.write_text(
        "OLLAMA_API_KEY=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "Provider: ollama" in result.stdout
    assert "Base URL: https://ollama.com/api" in result.stdout
    assert "Resolved provider credentials from $OLLAMA_API_KEY." in result.stdout


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

    monkeypatch.setattr(
        "foundation.cli._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

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


def test_chat_rejects_json_mode_without_a_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "chat", "--json"],
    )

    assert result.exit_code == 2
    assert "requires a request" in result.stdout


def test_chat_without_request_starts_interactive_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["summarize git status", "/exit"])
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session", lambda _settings: prompt_session
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "summarize git status"
    assert requests[0].conversation_history == []
    assert requests[0].cwd == (tmp_path / "workspace")
    assert "Interactive Chat" in result.stdout


def test_chat_interactive_shell_prefix_routes_to_direct_shell_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["!pwd", "/exit"])
    captured_commands: list[str] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session", lambda _settings: prompt_session
    )
    monkeypatch.setattr(
        "foundation.cli._execute_repl_shell_command",
        lambda **kwargs: captured_commands.append(kwargs["raw_command"]),
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert captured_commands == ["pwd"]


def test_chat_interactive_can_override_approval_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/approval auto", "summarize git status", "/exit"])
    captured_modes: list[ApprovalMode | None] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session", lambda _settings: prompt_session
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result(request.message)

    def _stub_build_orchestrator(_settings: object, **kwargs: object) -> StubOrchestrator:
        captured_modes.append(kwargs.get("approval_mode"))  # type: ignore[arg-type]
        return StubOrchestrator()

    monkeypatch.setattr("foundation.cli._build_orchestrator", _stub_build_orchestrator)

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert captured_modes == [ApprovalMode.AUTO]


def test_chat_interactive_persists_transcript_across_turns_and_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_sessions = [
        FakePromptSession(["summarize git status", "what about tests?", "/exit"]),
        FakePromptSession(["continue from before", "/exit"]),
    ]
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    first_result = runner.invoke(app, ["--config", str(config_path), "chat"])
    second_result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert [request.message for request in requests] == [
        "summarize git status",
        "what about tests?",
        "continue from before",
    ]
    assert requests[0].conversation_history == []
    assert len(requests[1].conversation_history) == 2
    assert requests[1].conversation_history[0].role is ProviderMessageRole.USER
    assert requests[1].conversation_history[0].content == "summarize git status"
    assert len(requests[2].conversation_history) == 4
    user_messages = [
        message.content
        for message in requests[2].conversation_history
        if message.role is ProviderMessageRole.USER
    ]
    assert user_messages == ["summarize git status", "what about tests?"]


def test_chat_interactive_reset_clears_persisted_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_sessions = [
        FakePromptSession(["summarize git status", "/reset", "/exit"]),
        FakePromptSession(["continue from before", "/exit"]),
    ]
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    first_result = runner.invoke(app, ["--config", str(config_path), "chat"])
    second_result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert requests[1].conversation_history == []


def test_chat_interactive_persists_shell_turns_into_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_sessions = [
        FakePromptSession(["!pwd", "/exit"]),
        FakePromptSession(["what did that command print?", "/exit"]),
    ]
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    first_result = runner.invoke(app, ["--config", str(config_path), "chat"])
    second_result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert len(requests) == 1
    assert len(requests[0].conversation_history) == 2
    assert requests[0].conversation_history[0].role is ProviderMessageRole.USER
    assert requests[0].conversation_history[0].content == "!pwd"
    assert requests[0].conversation_history[1].role is ProviderMessageRole.ASSISTANT
    assert "Direct shell command: `pwd`" in requests[0].conversation_history[1].content
    assert str(tmp_path / "workspace") in requests[0].conversation_history[1].content


def test_chat_interactive_manual_approval_history_stays_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["!touch manual.txt", "/exit"])
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli._build_chat_prompt_session",
        lambda _settings: prompt_session,
    )

    chat_result = runner.invoke(
        app,
        ["--config", str(config_path), "--approval-mode", "manual", "chat"],
    )
    history_result = runner.invoke(
        app,
        ["--config", str(config_path), "history", "--json"],
    )

    assert chat_result.exit_code == 0
    payload = json.loads(history_result.stdout)
    assert payload[0]["status"] == SessionStatus.PENDING_APPROVAL.value
    assert payload[0]["pending_approval_actions"] == 1


def test_build_chat_prompt_session_enables_multiline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    settings = load_settings(config_path=config_path)
    captured_kwargs: dict[str, object] = {}

    class FakePromptSessionImpl:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

    class FakeAutoSuggestFromHistory:
        pass

    class FakeNestedCompleter:
        @classmethod
        def from_nested_dict(cls, nested: dict[str, object]) -> FakeNestedCompleter:
            captured_kwargs["completions"] = nested
            return cls()

    class FakeFileHistory:
        def __init__(self, path: str) -> None:
            captured_kwargs["history_path"] = path

    class FakeKeyBindings:
        def __init__(self) -> None:
            self.bindings: list[tuple[str, ...]] = []

        def add(self, *keys: str) -> Callable[[object], object]:
            def _decorator(func: object) -> object:
                self.bindings.append(keys)
                return func

            return _decorator

    prompt_toolkit_module = cast(Any, types.ModuleType("prompt_toolkit"))
    prompt_toolkit_module.PromptSession = FakePromptSessionImpl
    auto_suggest_module = cast(Any, types.ModuleType("prompt_toolkit.auto_suggest"))
    auto_suggest_module.AutoSuggestFromHistory = FakeAutoSuggestFromHistory
    completion_module = cast(Any, types.ModuleType("prompt_toolkit.completion"))
    completion_module.NestedCompleter = FakeNestedCompleter
    history_module = cast(Any, types.ModuleType("prompt_toolkit.history"))
    history_module.FileHistory = FakeFileHistory
    key_binding_module = cast(Any, types.ModuleType("prompt_toolkit.key_binding"))
    key_binding_module.KeyBindings = FakeKeyBindings

    monkeypatch.setitem(sys.modules, "prompt_toolkit", prompt_toolkit_module)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.auto_suggest", auto_suggest_module)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.completion", completion_module)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.history", history_module)
    monkeypatch.setitem(sys.modules, "prompt_toolkit.key_binding", key_binding_module)

    _build_chat_prompt_session(settings)

    assert captured_kwargs["multiline"] is True


def test_render_interactive_chat_help_preserves_argument_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()
    test_console = Console(file=buffer, force_terminal=False, color_system=None, width=140)
    monkeypatch.setattr("foundation.cli.console", test_console)

    _render_interactive_chat_help()

    output = buffer.getvalue()
    assert "/history [limit]" in output
    assert "/config [locations]" in output
    assert "/cwd [path]" in output
    assert "/approval [auto|manual|prompt]" in output


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
