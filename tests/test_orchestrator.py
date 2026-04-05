from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from foundation.models import (
    ExecutionStatus,
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
    SessionStatus,
    UserRequest,
)
from foundation.services import ApprovalService, HistoryStore, LocalToolService, ShellRuntime
from foundation.services.orchestrator import OrchestrationPlanError, RequestOrchestrator
from foundation.settings import ApprovalMode


def _write_executable(path: Path, content: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n{textwrap.dedent(content)}",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: dict[str, str],
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in scripts.items():
        _write_executable(bin_dir / name, script)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


class StubProvider:
    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[ProviderPrompt] = []

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        self.calls.append(prompt)
        return self._responses.pop(0)


class CountingShellRuntime(ShellRuntime):
    def __init__(self, *, workspace_root: Path) -> None:
        super().__init__(
            workspace_root=workspace_root,
            default_timeout_seconds=2,
            max_timeout_seconds=10,
            capture_limit_kb=64,
        )
        self.calls = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return super().execute(*args, **kwargs)


def _provider_response(payload: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(payload),
        structured_output=payload,
        metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
    )


def _orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: StubProvider,
    *,
    scripts: dict[str, str] | None = None,
    approval_service: ApprovalService | None = None,
    history_store: HistoryStore | None = None,
    shell_output_callback: Any | None = None,
) -> tuple[RequestOrchestrator, CountingShellRuntime, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    if scripts:
        _install_binaries(tmp_path, monkeypatch, scripts)
    runtime = CountingShellRuntime(workspace_root=workspace_root)
    tool_service = LocalToolService(
        workspace_root=workspace_root,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    orchestrator = RequestOrchestrator(
        workspace_root=workspace_root,
        approval_mode=ApprovalMode.PROMPT,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        approval_service=approval_service,
        history_store=history_store,
        shell_output_callback=shell_output_callback,
    )
    return orchestrator, runtime, workspace_root


def test_orchestrator_executes_tool_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I will search the workspace first.",
                    "actions": [
                        {
                            "id": "search_workspace",
                            "kind": "tool_call",
                            "summary": "Search for the requested text",
                            "tool_call": {
                                "tool": "search",
                                "arguments": {
                                    "query": "needle",
                                    "max_results": 5,
                                },
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        scripts={
            "rg": """
                import json

                print(
                    json.dumps(
                        {
                            "type": "match",
                            "data": {
                                "path": {"text": "src/example.py"},
                                "lines": {"text": "needle found\\n"},
                                "line_number": 4,
                                "submatches": [{"start": 0, "end": 6}],
                            },
                        }
                    )
                )
            """,
        },
    )

    result = orchestrator.orchestrate(UserRequest(message="find needle"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert result.execution_results[0].artifact_type is not None
    assert result.execution_results[0].artifact_type.value == "search"
    assert result.execution_results[0].artifact is not None
    assert result.execution_results[0].artifact["matches"][0]["path"] == "src/example.py"
    assert result.summary.executed_actions == 1


def test_orchestrator_retries_invalid_plans_without_duplicate_shell_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Trying a shell command.",
                    "actions": [
                        {
                            "id": "bad_shell",
                            "kind": "shell",
                            "summary": "Bad shell action",
                            "shell": {
                                "command": "git status",
                            },
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Showing the current directory instead.",
                    "actions": [
                        {
                            "id": "show_cwd",
                            "kind": "shell",
                            "summary": "Show the current directory",
                            "shell": {
                                "command": "pwd",
                            },
                        }
                    ],
                }
            ),
        ]
    )
    orchestrator, runtime, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="where am I"))

    assert len(provider.calls) == 2
    assert runtime.calls == 1
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert result.execution_results[0].artifact is not None
    assert result.execution_results[0].artifact["stdout"] == f"{workspace_root}\n"


def test_orchestrator_marks_risky_shell_commands_as_pending_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "This would remove a file.",
                    "actions": [
                        {
                            "id": "remove_file",
                            "kind": "shell",
                            "summary": "Delete a file",
                            "shell": {
                                "command": "rm",
                                "args": ["example.txt"],
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="delete example.txt"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.PENDING_APPROVAL
    assert result.summary.pending_approval_actions == 1


def test_orchestrator_keeps_out_of_workspace_shell_reads_pending_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("secret\n", encoding="utf-8")
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I can inspect that file directly.",
                    "actions": [
                        {
                            "id": "read_outside_file",
                            "kind": "shell",
                            "summary": "Read a file outside the workspace",
                            "shell": {
                                "command": "cat",
                                "args": [str(outside_path)],
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="read the outside file"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.PENDING_APPROVAL
    assert result.summary.pending_approval_actions == 1


def test_orchestrator_keeps_environment_dump_pending_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I will inspect the environment.",
                    "actions": [
                        {
                            "id": "dump_environment",
                            "kind": "shell",
                            "summary": "Print the current environment",
                            "shell": {
                                "command": "env",
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="show all environment variables"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.PENDING_APPROVAL
    assert result.summary.pending_approval_actions == 1


def test_orchestrator_fails_visibly_on_invalid_tool_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_payload = {
        "assistant_message": "Searching with an unsupported argument.",
        "actions": [
            {
                "id": "bad_search",
                "kind": "tool_call",
                "summary": "Bad search request",
                "tool_call": {
                    "tool": "search",
                    "arguments": {
                        "query": "needle",
                        "unknown_flag": True,
                    },
                },
            }
        ],
    }
    provider = StubProvider(
        [
            _provider_response(invalid_payload),
            _provider_response(invalid_payload),
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    with pytest.raises(OrchestrationPlanError):
        orchestrator.orchestrate(UserRequest(message="find needle"))

    assert runtime.calls == 0
    assert len(provider.calls) == 2


def test_orchestrator_executes_risky_shell_commands_after_prompt_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I can create that file after approval.",
                    "actions": [
                        {
                            "id": "create_file",
                            "kind": "shell",
                            "summary": "Create a file in the workspace",
                            "shell": {
                                "command": "touch",
                                "args": ["approved.txt"],
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _request: True,
        ),
    )

    result = orchestrator.orchestrate(UserRequest(message="create approved.txt"))

    assert runtime.calls == 1
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert (workspace_root / "approved.txt").exists()


def test_orchestrator_streams_shell_output_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Streaming the current directory.",
                    "actions": [
                        {
                            "id": "show_cwd_stream",
                            "kind": "shell",
                            "summary": "Show the current directory with streaming output",
                            "shell": {
                                "command": "pwd",
                                "mode": "stream",
                            },
                        }
                    ],
                }
            )
        ]
    )
    streamed_chunks: list[str] = []
    orchestrator, runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        shell_output_callback=lambda event: streamed_chunks.append(event.text),
    )

    result = orchestrator.orchestrate(UserRequest(message="where am I"))

    assert runtime.calls == 1
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert "".join(streamed_chunks) == f"{workspace_root}\n"


def test_orchestrator_blocks_risky_shell_commands_when_prompt_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I can create that file after approval.",
                    "actions": [
                        {
                            "id": "create_file",
                            "kind": "shell",
                            "summary": "Create a file in the workspace",
                            "shell": {
                                "command": "touch",
                                "args": ["denied.txt"],
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _request: False,
        ),
    )

    result = orchestrator.orchestrate(UserRequest(message="create denied.txt"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.BLOCKED
    assert not (workspace_root / "denied.txt").exists()


def test_orchestrator_persists_sessions_commands_and_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Showing the current directory.",
                    "actions": [
                        {
                            "id": "show_cwd",
                            "kind": "shell",
                            "summary": "Show the current directory",
                            "shell": {
                                "command": "pwd",
                            },
                        }
                    ],
                }
            )
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator, _runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(UserRequest(message="where am I"))

    assert result.session_id is not None
    sessions = history_store.list_sessions(limit=5)
    assert sessions[0].session_id == result.session_id
    assert sessions[0].kind.value == "chat"

    detail = history_store.get_session(result.session_id)
    assert detail is not None
    assert detail.request_text == "where am I"
    assert detail.summary_text is not None
    assert detail.commands[0].command == "pwd"
    assert detail.commands[0].stdout == f"{workspace_root}\n"


def test_orchestrator_persists_pending_approval_session_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I can create that file after approval.",
                    "actions": [
                        {
                            "id": "create_file",
                            "kind": "shell",
                            "summary": "Create a file in the workspace",
                            "shell": {
                                "command": "touch",
                                "args": ["manual.txt"],
                            },
                        }
                    ],
                }
            )
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        approval_service=ApprovalService(mode=ApprovalMode.MANUAL),
        history_store=history_store,
    )

    result = orchestrator.orchestrate(UserRequest(message="create manual.txt"))

    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.PENDING_APPROVAL
    sessions = history_store.list_sessions(limit=5)
    assert sessions[0].status is SessionStatus.PENDING_APPROVAL

    detail = history_store.get_session(result.session_id or "")
    assert detail is not None
    assert detail.status is SessionStatus.PENDING_APPROVAL
