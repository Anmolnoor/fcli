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
import typer.main
from keyring.errors import NoKeyringError
from rich.console import Console
from typer.testing import CliRunner

from foundation.cli import (
    AgentInvocationMode,
    CLIRequestRoute,
    FoundationGroup,
    app,
)
from foundation.cli_interactive import (
    InteractiveChatState,
    _build_chat_prompt_session,
    _chat_prompt,
    _render_interactive_chat_help,
)
from foundation.models import (
    ActionKind,
    AssistantMessage,
    AssistantPlan,
    BrainSession,
    ContextSnapshot,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    ExecutionStep,
    LoopStopReason,
    OrchestrationResult,
    OrchestrationSummary,
    PlannedAction,
    PlanningStep,
    ProviderMessageRole,
    ProviderResponseMetadata,
    RenderMode,
    SelectionReason,
    SessionKind,
    SessionStatus,
    ToolCall,
    TraceArtifactRef,
    TraceArtifactRole,
    TraceEdge,
    TraceEdgeKind,
    UserRequest,
)
from foundation.services import SessionManager, TraceStore
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


def _chat_result_with_details(message: str) -> OrchestrationResult:
    return OrchestrationResult(
        session_id="session-quiet-1234",
        request=UserRequest(message=message),
        context=ContextSnapshot(
            workspace_root="/tmp/workspace",
            request_cwd="/tmp/workspace",
            approval_mode="prompt",
            available_tools=["fd", "git", "rg"],
        ),
        plan=AssistantPlan(
            assistant_message="I inspected the workspace context.",
            actions=[
                PlannedAction(
                    id="inspect_repo",
                    kind=ActionKind.TOOL_CALL,
                    summary="Discover Python files in the workspace.",
                    tool_call=ToolCall(
                        capability_id="foundation.files",
                        arguments={
                            "pattern": "py",
                            "scope": "/tmp/workspace",
                            "file_type": "file",
                            "max_results": 10,
                        },
                    ),
                )
            ],
        ),
        planning_metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
        policy_decisions=[],
        execution_results=[
            ExecutionResult(
                action_id="inspect_repo",
                status=ExecutionStatus.EXECUTED,
                summary="Executed capability `foundation.files` for action inspect_repo.",
                artifact_type=ExecutionArtifactType.FILES,
                artifact={
                    "tool": "fd",
                    "pattern": "py",
                    "scope": "/tmp/workspace",
                    "file_type": "file",
                    "paths": [
                        "src/foundation/cli.py",
                        "tests/test_cli.py",
                    ],
                    "truncated": False,
                },
            )
        ],
        assistant_message=AssistantMessage(content="I inspected the workspace context."),
        summary=OrchestrationSummary(
            executed_actions=1,
            pending_approval_actions=0,
            blocked_actions=0,
            failed_actions=0,
            skipped_actions=0,
            text="Executed 1 action and captured local workspace context.",
        ),
    )


def _seed_trace_session(config_path: Path) -> str:
    settings = load_settings(config_path=config_path)
    store = TraceStore(database_path=settings.history.database_path)
    session_id = store.start_session(
        kind=SessionKind.CHAT,
        workspace_root=settings.workspace_root,
        request_cwd=settings.workspace_root,
        approval_mode="prompt",
        request_text="where am I",
    )
    planning_step = PlanningStep(
        step_id="planning:req-seeded",
        trace_id=session_id,
        session_id=session_id,
        request_id="req-seeded",
        request_text="where am I",
        request_cwd=str(settings.workspace_root),
        candidate_capability_ids=["foundation.shell.command"],
        selection_reasons=[
            SelectionReason(
                selected_capability_id="foundation.shell.command",
                candidate_capability_ids=["foundation.shell.command"],
                summary="Show the current directory",
            )
        ],
        action_ids=["show_cwd"],
        planning_metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
        artifacts=[
            TraceArtifactRef(
                name="planning_context",
                role=TraceArtifactRole.CONTEXT,
                storage_ref="history://seed/context",
                preview="{}",
            )
        ],
        started_at="2026-04-06T00:00:00Z",
        completed_at="2026-04-06T00:00:01Z",
        duration_seconds=0.01,
    )
    execution_step = ExecutionStep(
        step_id="action:show_cwd",
        trace_id=session_id,
        session_id=session_id,
        request_id="req-seeded",
        action_id="show_cwd",
        action_summary="Show the current directory",
        status=ExecutionStatus.EXECUTED,
        request_cwd=str(settings.workspace_root),
        capability_id="foundation.shell.command",
        capability_version="1.0.0",
        capability_name="Shell Runtime",
        manifest_fingerprint="a" * 64,
        policy_snapshot_version="v2-stage2",
        selection_reason=SelectionReason(
            selected_capability_id="foundation.shell.command",
            candidate_capability_ids=["foundation.shell.command"],
            summary="Show the current directory",
        ),
        artifacts=[
            TraceArtifactRef(
                name="execution_result",
                role=TraceArtifactRole.OUTPUT,
                storage_ref="history://seed/output",
                preview='{"stdout": "/tmp/workspace"}',
            )
        ],
        side_effects=[],
        started_at="2026-04-06T00:00:01Z",
        completed_at="2026-04-06T00:00:02Z",
        duration_seconds=0.01,
    )
    store.record_trace_step(session_id, step=planning_step)
    store.record_trace_step(session_id, step=execution_step)
    store.record_trace_edge(
        session_id,
        edge=TraceEdge(
            trace_id=session_id,
            source_step_id=planning_step.step_id,
            target_step_id=execution_step.step_id,
            edge_kind=TraceEdgeKind.PLANNED,
        ),
    )
    store.finalize_session(session_id, status=SessionStatus.COMPLETED)
    return session_id


class FakePromptSession:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[Any] = []

    def prompt(self, message: Any) -> str:
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
    assert "foundation 0.2.0" in result.stdout


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
    # Stage 06: approval-boundary detail is visible for each capability.
    assert "foundation.git.commit@1.0.0" in result.stdout
    assert "risk=medium" in result.stdout
    assert "risk=low" in result.stdout
    assert "trust=foundation" in result.stdout


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


def test_doctor_allows_codex_chatgpt_login_without_openai_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[provider]\nname = "codex"\nmodel = "gpt-5.5"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "Provider: codex" in result.stdout
    assert "Model: gpt-5.5" in result.stdout
    assert "Base URL: codex://local" in result.stdout
    assert "Credentials required: no" in result.stdout
    assert "local Codex ChatGPT login" in result.stdout
    assert "API key is required" in result.stdout


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
        "foundation.cli_runtime._build_orchestrator",
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


def test_chat_one_shot_defaults_to_concise_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "chat", "inspect", "the", "workspace"],
    )

    assert result.exit_code == 0
    assert "I inspected the workspace context." in result.stdout


def test_chat_one_shot_accepts_no_live_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global --no-live flag is accepted and a one-shot turn still runs."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def set_event_sink(self, _sink):
            pass

        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "--no-live",
            "--no-monitor",
            "chat",
            "look",
            "around",
        ],
    )

    assert result.exit_code == 0
    assert "I inspected the workspace context." in result.stdout


def test_chat_one_shot_writes_event_log_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-shot turn writes a redacted NDJSON file under events_dir."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def __init__(self) -> None:
            self._sink = None

        def set_event_sink(self, sink) -> None:
            self._sink = sink

        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            assert self._sink is not None
            self._sink(
                "user_request",
                {"request_id": "r1", "request_text": request.message},
            )
            self._sink(
                "session_start",
                {"request_id": "r1", "session_id": "sess-cli"},
            )
            self._sink(
                "tool_call_started",
                {"request_id": "r1", "session_id": "sess-cli", "tool": "git"},
            )
            self._sink(
                "session_end",
                {
                    "request_id": "r1",
                    "session_id": "sess-cli",
                    "status": "completed",
                },
            )
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "--no-live", "chat", "do", "it"],
    )

    assert result.exit_code == 0
    events_dir = tmp_path / "state" / "events"
    log_path = events_dir / "sess-cli.ndjson"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "session_start" in contents
    assert "session_end" in contents
    assert (events_dir / "sessions.jsonl").exists()


def test_chat_one_shot_no_monitor_writes_no_event_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def set_event_sink(self, _sink) -> None:
            pass

        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "--no-live",
            "--no-monitor",
            "chat",
            "do",
            "it",
        ],
    )

    assert result.exit_code == 0
    events_dir = tmp_path / "state" / "events"
    assert not events_dir.exists() or list(events_dir.glob("*.ndjson")) == []
    assert not (events_dir / "sessions.jsonl").exists()
    assert "Executed 1 action and captured local workspace context." in result.stdout
    assert "Path preview:" in result.stdout
    assert "Planned Actions" not in result.stdout
    assert "Orchestration Summary" not in result.stdout


def test_chat_one_shot_verbose_rendering_shows_internal_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "chat",
            "--render",
            "verbose",
            "inspect",
            "the",
            "workspace",
        ],
    )

    assert result.exit_code == 0
    assert "Planned Actions" in result.stdout
    assert "Orchestration Summary" in result.stdout
    assert "Path Discovery" in result.stdout


def test_chat_without_request_starts_interactive_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["summarize git status", "/exit"])
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "summarize git status"
    assert requests[0].conversation_history == []
    assert requests[0].cwd == (tmp_path / "workspace")
    assert "Interactive Chat" in result.stdout
    assert "Commands:" in result.stdout
    assert "Prompt history:" not in result.stdout


def test_chat_interactive_shell_prefix_routes_to_direct_shell_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["!pwd", "/exit"])
    captured_commands: list[str] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )
    monkeypatch.setattr(
        "foundation.cli_interactive._execute_repl_shell_command",
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
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result(request.message)

    def _stub_build_orchestrator(_settings: object, **kwargs: object) -> StubOrchestrator:
        captured_modes.append(kwargs.get("approval_mode"))  # type: ignore[arg-type]
        return StubOrchestrator()

    monkeypatch.setattr("foundation.cli_runtime._build_orchestrator", _stub_build_orchestrator)

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
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
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


def test_chat_interactive_detail_commands_render_last_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(
        ["inspect workspace", "/plan", "/actions", "/summary", "/exit"]
    )
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            return _chat_result_with_details(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert "Path preview:" in result.stdout
    assert "Planned Actions" in result.stdout
    assert "Action inspect_repo" in result.stdout
    assert "Orchestration Summary" in result.stdout


def test_chat_interactive_detail_commands_require_a_prior_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/summary", "/exit"])
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert "No assistant turn has completed in this session yet." in result.stdout


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
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
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
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
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
        "foundation.cli_interactive._build_chat_prompt_session",
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


def test_chat_recovery_notice_does_not_replay_full_interrupted_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/exit"])
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_session,
    )
    settings = load_settings(config_path=config_path)
    session_manager = SessionManager(
        database_path=settings.app.state_dir / "chat-sessions.sqlite3",
        workspace_root=settings.workspace_root,
        config_dir=settings.config_path.parent,
        provider_name=settings.provider.name,
    )
    session = session_manager.create_session(
        initial_cwd=settings.workspace_root,
        approval_mode="prompt",
        model=settings.provider.model,
    )
    interrupted = (
        "can you help me with learning; i want you to create a github cheat sheet "
        "in the developer dir with most common 20 commands ;"
    )
    session_manager.mark_turn_started(
        session,
        turn_kind="chat",
        user_message=interrupted,
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert "Recovered after an interrupted turn; unfinished request was not rerun." in result.stdout
    assert "Recovered the last clean checkpoint" not in result.stdout
    assert interrupted not in result.stdout


def test_trace_command_lists_seeded_traces_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    session_id = _seed_trace_session(config_path)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "trace", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["trace_id"] == session_id
    assert payload[0]["step_count"] == 2
    assert payload[0]["selected_capability_ids"] == ["foundation.shell.command"]


def test_trace_command_emits_audit_report_for_one_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    session_id = _seed_trace_session(config_path)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "trace",
            "--session",
            session_id,
            "--step",
            "action:show_cwd",
            "--predecessors",
            "--audit",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["inspected_step_id"] == "action:show_cwd"
    assert payload["completeness_passed"] is False
    assert payload["missing_fields_by_step"]["action:show_cwd"] == ["policy_evaluation"]
    assert len(payload["steps"]) == 2


def test_chat_interactive_memory_command_updates_project_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(
        [
            "/memory append project Always run tests before merge.",
            "/memory show project",
            "/exit",
        ]
    )
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_session,
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert "Always run tests before merge." in result.stdout
    assert (tmp_path / "workspace" / "FOUNDATION.md").read_text(
        encoding="utf-8"
    ) == "Always run tests before merge.\n"


def test_chat_interactive_model_command_updates_subsequent_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/model gpt-5", "summarize git status", "/exit"])
    captured_models: list[str] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_session,
    )

    def _stub_execute_chat_request(
        settings: object,
        *,
        message: str,
        conversation_history: list[object] | None = None,
        cwd: Path | None,
        plan_only: bool,
        approval_mode: ApprovalMode | None = None,
        shell_output_callback: object | None = None,
        **_kwargs: Any,
    ) -> OrchestrationResult:
        captured_models.append(cast(Any, settings).provider.model)
        return _chat_result(message)

    monkeypatch.setattr(
        "foundation.cli_interactive._execute_chat_request",
        _stub_execute_chat_request,
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert captured_models == ["gpt-5"]


def test_chat_interactive_can_resume_specific_persistent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_stage_2_config(tmp_path)
    prompt_sessions = [
        FakePromptSession(["first thread", "/exit"]),
        FakePromptSession(["second thread", "/exit"]),
        FakePromptSession(["continue first thread", "/exit"]),
    ]
    requests: list[UserRequest] = []
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session",
        lambda _settings: prompt_sessions.pop(0),
    )

    def _stub_execute_chat_request(
        _settings: object,
        *,
        message: str,
        conversation_history: list[object] | None = None,
        cwd: Path | None,
        plan_only: bool,
        approval_mode: ApprovalMode | None = None,
        shell_output_callback: object | None = None,
        **_kwargs: Any,
    ) -> OrchestrationResult:
        requests.append(
            UserRequest(
                message=message,
                conversation_history=cast(list[Any], conversation_history or []),
                cwd=cwd,
                plan_only=plan_only,
            )
        )
        return _chat_result(message)

    monkeypatch.setattr(
        "foundation.cli_interactive._execute_chat_request",
        _stub_execute_chat_request,
    )

    first_result = runner.invoke(app, ["--config", str(config_path), "chat", "--new"])
    second_result = runner.invoke(app, ["--config", str(config_path), "chat", "--new"])

    settings = load_settings(config_path=config_path)
    session_manager = SessionManager(
        database_path=settings.app.state_dir / "chat-sessions.sqlite3",
        workspace_root=settings.workspace_root,
        config_dir=settings.config_path.parent,
        provider_name=settings.provider.name,
    )
    sessions = session_manager.list_sessions(limit=10)
    explicit_session_id = sessions[1].session_id

    third_result = runner.invoke(
        app,
        ["--config", str(config_path), "chat", "--resume", explicit_session_id],
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert third_result.exit_code == 0
    assert [request.message for request in requests] == [
        "first thread",
        "second thread",
        "continue first thread",
    ]
    user_messages = [
        message.content
        for message in requests[2].conversation_history
        if message.role is ProviderMessageRole.USER
    ]
    assert user_messages == ["first thread"]


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
            self.handlers: dict[tuple[str, ...], Callable[[object], object]] = {}

        def add(self, *keys: str) -> Callable[[object], object]:
            def _decorator(func: object) -> object:
                self.bindings.append(keys)
                self.handlers[keys] = cast(Callable[[object], object], func)
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
    bindings = cast(FakeKeyBindings, captured_kwargs["key_bindings"])
    assert ("enter",) in bindings.bindings
    assert ("escape", "enter") in bindings.bindings

    class FakeBuffer:
        def __init__(self) -> None:
            self.submitted = False
            self.inserted_text: list[str] = []

        def validate_and_handle(self) -> None:
            self.submitted = True

        def insert_text(self, value: str) -> None:
            self.inserted_text.append(value)

    class FakeEvent:
        def __init__(self, current_buffer: FakeBuffer) -> None:
            self.current_buffer = current_buffer

    submit_buffer = FakeBuffer()
    bindings.handlers[("enter",)](FakeEvent(submit_buffer))
    assert submit_buffer.submitted is True

    newline_buffer = FakeBuffer()
    bindings.handlers[("escape", "enter")](FakeEvent(newline_buffer))
    assert newline_buffer.inserted_text == ["\n"]


def test_chat_prompt_is_compact_with_padding_and_line_gap(tmp_path: Path) -> None:
    config_path = _write_stage_2_config(tmp_path)
    settings = load_settings(config_path=config_path)
    session = BrainSession(
        session_id="ac46fdcd1144487c9013b1262416974e",
        workspace_root=str(settings.workspace_root),
        initial_cwd=str(settings.workspace_root),
        current_cwd=str(settings.workspace_root),
        approval_mode="prompt",
        provider_name="ollama",
        model="glm-5.1:cloud",
        summary_text="",
        recent_turns=[],
        turn_count=0,
        created_at="2026-06-04T00:00:00Z",
        updated_at="2026-06-04T00:00:00Z",
    )
    state = InteractiveChatState(session=session)

    assert _chat_prompt(state, settings=settings, plan_only=False) == [
        ("", "\n  "),
        ("#22c7d6 bold", "fcli"),
        ("#22c7d6 bold", "> "),
    ]

    src_dir = settings.workspace_root / "src"
    src_dir.mkdir()
    state.current_cwd = src_dir

    assert _chat_prompt(state, settings=settings, plan_only=True) == [
        ("", "\n  "),
        ("#22c7d6 bold", "fcli"),
        ("", " src plan"),
        ("#22c7d6 bold", "> "),
    ]


def test_render_interactive_chat_help_preserves_argument_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()
    test_console = Console(file=buffer, force_terminal=False, color_system=None, width=140)
    monkeypatch.setattr("foundation.cli_interactive.console", test_console)

    _render_interactive_chat_help()

    output = buffer.getvalue()
    assert "Submit input: press Enter" in output
    assert "Insert newline: press Esc then Enter" in output
    assert "/history [limit]" in output
    assert "/plan: inspect the last structured plan" in output
    assert "/actions: inspect the last action results" in output
    assert "/summary: inspect the last orchestration summary" in output
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


# ---------------------------------------------------------------------------
# Stage 01 — Shell Entrypoint and Routing
# ---------------------------------------------------------------------------


def test_routing_models_are_importable() -> None:
    """AgentInvocationMode and CLIRequestRoute are part of the public API."""
    assert AgentInvocationMode.INTERACTIVE.value == "interactive"
    assert AgentInvocationMode.ONE_SHOT.value == "one_shot"
    assert CLIRequestRoute.ADMIN_SUBCOMMAND.value == "admin_subcommand"
    assert CLIRequestRoute.AGENT_INTERACTIVE.value == "agent_interactive"
    assert CLIRequestRoute.AGENT_ONE_SHOT.value == "agent_one_shot"


def test_foundation_group_is_used_by_app() -> None:
    """The top-level Typer app uses FoundationGroup for routing."""
    click_app = typer.main.get_command(app)
    assert isinstance(click_app, FoundationGroup)


def test_bare_foundation_starts_interactive_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running `foundation` with no args opens the interactive shell."""
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/exit"])
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    result = runner.invoke(app, ["--config", str(config_path)])

    assert result.exit_code == 0
    assert "Interactive Chat" in result.stdout


def test_bare_foundation_one_shot_routes_to_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running `foundation <request>` runs a one-shot agent turn."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    requests: list[UserRequest] = []

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "fix", "the", "failing", "test"],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "fix the failing test"


def test_bare_foundation_one_shot_quoted_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running `foundation 'fix the bug'` (single token) routes correctly."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    requests: list[UserRequest] = []

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "fix the failing test"],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "fix the failing test"


def test_admin_run_keeps_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation run` must route to the admin `run` subcommand."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "run"],
    )

    # The `run` subcommand without a command arg gives exit code 2
    assert result.exit_code == 2
    assert "No command provided" in result.stdout


def test_admin_config_keeps_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation config` routes to admin config, not agent one-shot."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "config"],
    )

    assert result.exit_code == 0
    # Config show emits JSON with settings
    payload = json.loads(result.stdout)
    assert "settings" in payload


def test_admin_doctor_keeps_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation doctor` routes to admin doctor, not agent."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "doctor"],
        env={"OPENAI_API_KEY": "test-key"},
    )

    # Doctor runs and reports status — exit code 0 (healthy) or 1 (issues).
    # It was dispatched to the admin subcommand, not the agent, so it
    # contains doctor-specific output (PASS/FAIL lines).
    assert result.exit_code in {0, 1}
    assert "PASS" in result.stdout or "FAIL" in result.stdout


def test_quoted_run_tests_routes_to_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation 'run tests'` (quoted) is a one-shot agent request, not admin."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    requests: list[UserRequest] = []

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "run tests"],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "run tests"


def test_chat_alias_parity_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation chat` still opens the interactive shell."""
    config_path = _write_stage_2_config(tmp_path)
    prompt_session = FakePromptSession(["/exit"])
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    monkeypatch.setattr(
        "foundation.cli_interactive._build_chat_prompt_session", lambda _settings: prompt_session
    )

    result = runner.invoke(app, ["--config", str(config_path), "chat"])

    assert result.exit_code == 0
    assert "Interactive Chat" in result.stdout


def test_chat_alias_parity_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation chat <request>` runs the same one-shot path."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    requests: list[UserRequest] = []

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "chat", "fix", "the", "bug"],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "fix the bug"


def test_empty_string_request_fails_with_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`foundation ''` should fail with a clear usage error."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)

    result = runner.invoke(
        app,
        ["--config", str(config_path), ""],
    )

    assert result.exit_code == 2
    assert "Empty request text" in result.stdout


def test_version_flag_still_works() -> None:
    """`foundation --version` still shows the version."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "foundation 0.2.0" in result.stdout


def test_help_flag_still_works() -> None:
    """`foundation --help` still shows help."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "foundation" in result.stdout.lower()


def test_global_flags_not_swallowed_as_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global flags like --debug should not appear in request text."""
    config_path = _write_stage_2_config(tmp_path)
    monkeypatch.setattr("foundation.settings.keyring.get_password", lambda *_args: None)
    requests: list[UserRequest] = []

    class StubOrchestrator:
        def orchestrate(self, request: UserRequest) -> OrchestrationResult:
            requests.append(request)
            return _chat_result(request.message)

    monkeypatch.setattr(
        "foundation.cli_runtime._build_orchestrator",
        lambda _settings, **_kwargs: StubOrchestrator(),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "--debug", "fix the bug"],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].message == "fix the bug"
    assert "--debug" not in requests[0].message


# ------------------------------------------------------------------
# Stage 05: Iteration-aware concise notices
# ------------------------------------------------------------------


def _iteration_result_for_notices(
    *,
    changed_paths: list[str] | None = None,
    shell_commands: list[tuple[str, list[str]]] | None = None,
    verification: dict[str, Any] | None = None,
    stop_reason: Any = None,
    pending_approval_actions: int = 0,
) -> OrchestrationResult:
    """Build an OrchestrationResult with configurable iteration data."""
    from foundation.models import (
        IterationObservation,
        LoopStopReason,
        OrchestrationIteration,
        ShellAction,
        VerificationNotice,
    )

    changed_paths = changed_paths or []
    shell_commands = shell_commands or []

    actions: list[PlannedAction] = []
    execution_results: list[ExecutionResult] = []

    for idx, path in enumerate(changed_paths):
        action_id = f"write_{idx}"
        actions.append(
            PlannedAction(
                id=action_id,
                kind=ActionKind.TOOL_CALL,
                summary=f"Write {path}",
                tool_call=ToolCall(
                    capability_id="foundation.file.write",
                    arguments={"path": path, "content": ""},
                ),
            )
        )
        execution_results.append(
            ExecutionResult(
                action_id=action_id,
                status=ExecutionStatus.EXECUTED,
                summary=f"Wrote {path}.",
                artifact_type=ExecutionArtifactType.FILE_WRITE,
                artifact={"path": path},
            )
        )

    for idx, (cmd, args) in enumerate(shell_commands):
        action_id = f"shell_{idx}"
        actions.append(
            PlannedAction(
                id=action_id,
                kind=ActionKind.SHELL,
                summary=f"Run {cmd}",
                shell=ShellAction(command=cmd, args=args),
            )
        )
        execution_results.append(
            ExecutionResult(
                action_id=action_id,
                status=ExecutionStatus.EXECUTED,
                summary=f"Ran {cmd}.",
                artifact_type=ExecutionArtifactType.SHELL,
                artifact={
                    "ok": True,
                    "exit_code": 0,
                    "duration_seconds": 0.01,
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            )
        )

    plan = AssistantPlan(
        assistant_message="Working on it.",
        actions=actions,
    )
    context = ContextSnapshot(
        workspace_root="/tmp/workspace",
        request_cwd="/tmp/workspace",
        approval_mode="prompt",
    )
    planning_metadata = ProviderResponseMetadata(
        provider="stub",
        model="stub-model",
        latency_seconds=0.01,
    )

    iteration = OrchestrationIteration(
        iteration=1,
        context=context,
        plan=plan,
        planning_metadata=planning_metadata,
        policy_decisions=[],
        policy_evaluations=[],
        execution_results=execution_results,
        observation=IterationObservation(
            iteration=1,
            changed_paths=changed_paths,
            remaining_iterations=3,
            remaining_actions=20,
        ),
    )

    verification_notice: VerificationNotice | None = None
    if verification is not None:
        verification_notice = VerificationNotice(**verification)

    return OrchestrationResult(
        session_id="sess-notices-1",
        request=UserRequest(message="do stuff"),
        context=context,
        plan=plan,
        planning_metadata=planning_metadata,
        policy_decisions=[],
        execution_results=execution_results,
        assistant_message=AssistantMessage(content="Done."),
        summary=OrchestrationSummary(
            executed_actions=len(execution_results),
            pending_approval_actions=pending_approval_actions,
            blocked_actions=0,
            failed_actions=0,
            skipped_actions=0,
            text="Executed actions.",
        ),
        iterations=[iteration],
        stop_reason=stop_reason or LoopStopReason.ZERO_ACTION_PLAN,
        verification_notice=verification_notice,
    )


def test_interactive_concise_chat_turn_indents_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation.cli_rendering import _render_concise_chat_turn
    from foundation.models import ChatSurfacePolicy

    buffer = StringIO()
    test_console = Console(file=buffer, force_terminal=False, color_system=None, width=140)
    monkeypatch.setattr("foundation.cli_rendering.console", test_console)
    result = _iteration_result_for_notices(
        stop_reason=LoopStopReason.PENDING_APPROVAL,
        pending_approval_actions=1,
    )

    _render_concise_chat_turn(
        result,
        policy=ChatSurfacePolicy(render_mode=RenderMode.CONCISE),
        interactive=True,
    )

    output = buffer.getvalue()
    lines = output.splitlines()
    assert "  Done." in lines
    assert any(line.startswith("  Approval required for 1 action.") for line in lines)


def test_iteration_changed_files_notice_lists_paths() -> None:
    from foundation.cli_rendering import _iteration_changed_files_notice

    result = _iteration_result_for_notices(
        changed_paths=["src/a.py", "src/b.py"],
    )
    notice = _iteration_changed_files_notice(result)
    assert notice is not None
    assert "src/a.py" in notice.text
    assert "src/b.py" in notice.text
    assert notice.text.startswith("Changed files:")


def test_iteration_changed_files_notice_dedups_and_caps() -> None:
    from foundation.cli_rendering import _iteration_changed_files_notice

    # Build a result with execution_results spanning 10 unique writes plus a
    # duplicate, without tripping the per-plan 5-action cap.  The notice
    # builder reads `result.execution_results` directly, not the plan, so we
    # can attach many mutations.
    result = _iteration_result_for_notices(changed_paths=["src/keep.py"])
    extra_paths = [f"src/f{i}.py" for i in range(10)] + ["src/f0.py"]
    for idx, path in enumerate(extra_paths):
        result.execution_results.append(
            ExecutionResult(
                action_id=f"extra_{idx}",
                status=ExecutionStatus.EXECUTED,
                summary=f"Wrote {path}.",
                artifact_type=ExecutionArtifactType.FILE_WRITE,
                artifact={"path": path},
            )
        )
    notice = _iteration_changed_files_notice(result)
    assert notice is not None
    # 11 unique: "src/keep.py" + 10 distinct src/fN.py, cap 6 → 5 hidden
    assert "+5 more" in notice.text


def test_iteration_changed_files_notice_empty() -> None:
    from foundation.cli_rendering import _iteration_changed_files_notice

    result = _iteration_result_for_notices()
    assert _iteration_changed_files_notice(result) is None


def test_awaiting_input_notice_surfaces_question() -> None:
    from foundation.cli_rendering import _awaiting_input_notice
    from foundation.models import LoopStopReason

    result = _iteration_result_for_notices(stop_reason=LoopStopReason.AWAITING_USER_INPUT)
    result.execution_results.append(
        ExecutionResult(
            action_id="ask",
            status=ExecutionStatus.AWAITING_INPUT,
            summary="Which format?",
            artifact_type=ExecutionArtifactType.QUESTION,
            artifact={"question": "Which format?", "answer": None},
        )
    )
    notice = _awaiting_input_notice(result)
    assert notice is not None
    assert "Which format?" in notice.text


def test_awaiting_input_notice_none_without_stop_reason() -> None:
    from foundation.cli_rendering import _awaiting_input_notice

    result = _iteration_result_for_notices()
    assert _awaiting_input_notice(result) is None


def test_prompt_for_question_selects_option_by_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer as _typer

    from foundation.cli_runtime import _prompt_for_question
    from foundation.models import QuestionAction

    monkeypatch.setattr(_typer, "prompt", lambda *_a, **_k: "2")
    answer = _prompt_for_question(QuestionAction(prompt="Which format?", options=["json", "yaml"]))
    assert answer == "yaml"


def test_prompt_for_question_accepts_free_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer as _typer

    from foundation.cli_runtime import _prompt_for_question
    from foundation.models import QuestionAction

    monkeypatch.setattr(_typer, "prompt", lambda *_a, **_k: "  use msgpack  ")
    answer = _prompt_for_question(QuestionAction(prompt="Which format?"))
    assert answer == "use msgpack"


def test_iteration_commands_notice_lists_and_dedups_consecutive() -> None:
    from foundation.cli_rendering import _iteration_commands_notice

    result = _iteration_result_for_notices(
        shell_commands=[
            ("pytest", ["-x"]),
            ("pytest", ["-x"]),  # consecutive dup, should collapse
            ("ruff", ["check"]),
        ]
    )
    notice = _iteration_commands_notice(result)
    assert notice is not None
    assert notice.text.count("$ pytest -x") == 1
    assert "$ ruff check" in notice.text


def test_verification_outcome_notice_covers_all_outcomes() -> None:
    from foundation.cli_rendering import _verification_outcome_notice
    from foundation.models import VerificationOutcome

    passed = _iteration_result_for_notices(
        verification={
            "outcome": VerificationOutcome.PASSED,
            "verification_commands_run": ["pytest"],
        },
    )
    notice = _verification_outcome_notice(passed)
    assert notice is not None
    assert "passed" in notice.text
    assert "pytest" in notice.text

    failed = _iteration_result_for_notices(
        verification={
            "outcome": VerificationOutcome.FAILED,
            "verification_commands_run": ["pytest"],
        },
    )
    notice = _verification_outcome_notice(failed)
    assert notice is not None
    assert "failed" in notice.text

    unavailable = _iteration_result_for_notices(
        verification={
            "outcome": VerificationOutcome.UNAVAILABLE,
            "verification_commands_run": ["pytest"],
        },
    )
    notice = _verification_outcome_notice(unavailable)
    assert notice is not None
    assert "unavailable" in notice.text

    not_attempted = _iteration_result_for_notices(
        verification={
            "outcome": VerificationOutcome.NOT_ATTEMPTED,
            "verification_commands_run": [],
        },
    )
    notice = _verification_outcome_notice(not_attempted)
    assert notice is not None
    assert "no verification command" in notice.text


def test_approval_required_notice_fires_only_for_pending_approval_stop() -> None:
    from foundation.cli_rendering import _approval_required_notice
    from foundation.models import LoopStopReason

    pending = _iteration_result_for_notices(
        stop_reason=LoopStopReason.PENDING_APPROVAL,
        pending_approval_actions=2,
    )
    notice = _approval_required_notice(pending)
    assert notice is not None
    assert "2 actions" in notice.text

    completed = _iteration_result_for_notices(
        stop_reason=LoopStopReason.ZERO_ACTION_PLAN,
    )
    assert _approval_required_notice(completed) is None
