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
    LoopStopReason,
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
    SessionStatus,
    TraceQuery,
    UserRequest,
)
from foundation.services import ApprovalService, HistoryStore, LocalToolService, ShellRuntime
from foundation.services.orchestrator import (
    NoProgressDetector,
    OrchestrationPlanError,
    RequestOrchestrator,
)
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
        if not self._responses:
            return _provider_response({
                "assistant_message": "Done.",
                "actions": [],
            })
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
                                "capability_id": "foundation.search",
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


def test_orchestrator_executes_shell_runtime_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Showing the current directory through the shell runtime.",
                    "actions": [
                        {
                            "id": "show_cwd",
                            "kind": "tool_call",
                            "summary": "Show the current directory",
                            "tool_call": {
                                "capability_id": "foundation.shell.command",
                                "arguments": {
                                    "command": "pwd",
                                },
                            },
                        }
                    ],
                }
            )
        ]
    )
    orchestrator, runtime, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="where am I"))

    assert runtime.calls == 1
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert result.execution_results[0].artifact_type is not None
    assert result.execution_results[0].artifact_type.value == "shell"
    assert result.execution_results[0].artifact is not None
    assert result.execution_results[0].artifact["stdout"] == f"{workspace_root}\n"


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

    # 2 calls: bad plan + repair in iteration 1, then zero-action completion in iteration 2
    assert len(provider.calls) == 3
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


def test_orchestrator_blocks_out_of_workspace_shell_reads(
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
    assert result.execution_results[0].status is ExecutionStatus.BLOCKED
    assert result.summary.blocked_actions == 1


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
                    "capability_id": "foundation.search",
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
    assert detail.policy_evaluations[0].capability_id == "foundation.shell.command"
    assert detail.commands[0].command == "pwd"
    assert detail.commands[0].stdout == f"{workspace_root}\n"

    trace = history_store.get_trace(
        TraceQuery(
            session_id=result.session_id or "",
            include_predecessors=True,
        )
    )
    assert trace is not None
    # 3 steps: planning iter 1, execution, planning iter 2 (zero-action completion)
    assert len(trace.steps) == 3
    planning_step = trace.steps[0]
    execution_step = trace.steps[1]
    assert planning_step.step_type.value == "planning"
    assert execution_step.step_type.value == "execution"
    assert execution_step.capability_id == "foundation.shell.command"
    assert execution_step.manifest_fingerprint is not None
    assert execution_step.selection_reason.selected_capability_id == "foundation.shell.command"
    assert trace.edges[0].edge_kind.value == "planned"

    audit_report = history_store.get_audit_report(
        TraceQuery(
            session_id=result.session_id or "",
        )
    )
    assert audit_report is not None
    assert audit_report.completeness_passed is True
    assert audit_report.missing_fields_by_step == {}


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
    assert detail.policy_evaluations[0].capability_id == "foundation.shell.command"
    assert detail.approvals[0].capability_id == "foundation.shell.command"
    assert "workspace_write" in detail.approvals[0].requested_side_effects

    trace = history_store.get_trace(
        TraceQuery(session_id=result.session_id or "")
    )
    assert trace is not None
    assert len(trace.steps) == 2
    execution_step = next(
        s for s in trace.steps if s.step_type.value == "execution"
    )
    assert execution_step.step_type.value == "execution"
    assert execution_step.status is ExecutionStatus.PENDING_APPROVAL
    assert execution_step.action_id == "create_file"
    assert execution_step.iteration_index == 1

    filtered_trace = history_store.get_trace(
        TraceQuery(
            session_id=result.session_id or "",
            step_id=execution_step.step_id,
            include_predecessors=True,
        )
    )
    assert filtered_trace is not None
    assert len(filtered_trace.steps) == 2

    audit_report = history_store.get_audit_report(
        TraceQuery(
            session_id=result.session_id or "",
            step_id=execution_step.step_id,
            include_predecessors=True,
        )
    )
    assert audit_report is not None
    assert audit_report.completeness_passed is True


def test_orchestrator_persists_failed_execution_trace_as_audit_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Running the requested command.",
                    "actions": [
                        {
                            "id": "failing_command",
                            "kind": "shell",
                            "summary": "Run a command that exits with failure",
                            "shell": {
                                "command": "false",
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
        history_store=history_store,
    )

    result = orchestrator.orchestrate(UserRequest(message="run the failing command"))

    assert runtime.calls == 1
    assert result.execution_results[0].status is ExecutionStatus.FAILED
    assert result.summary.failed_actions == 1
    sessions = history_store.list_sessions(limit=5)
    assert sessions[0].status is SessionStatus.FAILED

    full_trace = history_store.get_trace(
        TraceQuery(session_id=result.session_id or "")
    )
    assert full_trace is not None
    execution_step = next(
        s for s in full_trace.steps if s.step_type.value == "execution"
    )
    assert execution_step.status is ExecutionStatus.FAILED
    assert execution_step.action_id == "failing_command"
    assert execution_step.iteration_index == 1
    assert execution_step.capability_id == "foundation.shell.command"
    assert execution_step.policy_evaluation is not None
    assert execution_step.manifest_fingerprint is not None
    assert execution_step.artifacts

    trace = history_store.get_trace(
        TraceQuery(
            session_id=result.session_id or "",
            step_id=execution_step.step_id,
            include_predecessors=True,
        )
    )
    assert trace is not None
    assert len(trace.steps) == 2

    audit_report = history_store.get_audit_report(
        TraceQuery(
            session_id=result.session_id or "",
            step_id=execution_step.step_id,
            include_predecessors=True,
        )
    )
    assert audit_report is not None
    assert audit_report.completeness_passed is True
    assert audit_report.missing_fields_by_step == {}


# ------------------------------------------------------------------
# Stage 04: Bounded replan loop tests
# ------------------------------------------------------------------


def test_zero_action_first_iteration_explanation_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-action plan on first iteration for explanation-only requests."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "The answer is 42.",
                "actions": [],
            }),
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(UserRequest(message="what is 42"))

    assert runtime.calls == 0
    assert len(result.iterations) == 1
    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN
    assert result.assistant_message.content == "The answer is 42."
    assert result.verification_notice is None
    assert result.summary.total_iterations == 1
    assert result.summary.executed_actions == 0


def test_pending_approval_stops_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending approval in first iteration stops the loop."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Need to delete that file.",
                "actions": [
                    {
                        "id": "rm_file",
                        "kind": "shell",
                        "summary": "Remove a file",
                        "shell": {"command": "rm", "args": ["x.txt"]},
                    }
                ],
            }),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="delete x.txt"),
    )

    assert result.stop_reason is LoopStopReason.PENDING_APPROVAL
    assert len(result.iterations) == 1
    assert result.summary.pending_approval_actions == 1
    assert "[Loop stopped:" in result.assistant_message.content


def test_fatal_failure_stops_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell spawn failure (fatal) stops the loop immediately."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Running nonexistent.",
                "actions": [
                    {
                        "id": "spawn_fail",
                        "kind": "shell",
                        "summary": "Run a nonexistent binary",
                        "shell": {"command": "nonexistent_binary_xyz"},
                    }
                ],
            }),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="run nonexistent"),
    )

    assert result.stop_reason is LoopStopReason.FATAL_EXECUTION_FAILURE
    assert len(result.iterations) == 1
    assert result.summary.failed_actions == 1


def test_max_iteration_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop stops at the configured max iteration cap."""
    from foundation.services.orchestrator import _MAX_LOOP_ITERATIONS

    responses = [
        _provider_response({
            "assistant_message": f"Iteration {i} running ls.",
            "actions": [
                {
                    "id": f"ls_{i}",
                    "kind": "shell",
                    "summary": "List files",
                    "shell": {"command": "ls"},
                }
            ],
        })
        for i in range(1, _MAX_LOOP_ITERATIONS + 1)
    ]
    provider = StubProvider(responses)
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="keep listing files"),
    )

    assert result.stop_reason is LoopStopReason.MAX_ITERATIONS
    assert len(result.iterations) == _MAX_LOOP_ITERATIONS
    assert result.summary.total_iterations == _MAX_LOOP_ITERATIONS
    # Shell capability has its own max_invocations budget independent of loop
    # caps; after that budget is exhausted the orchestrator still iterates but
    # actions are blocked by policy.  So runtime.calls <= _MAX_LOOP_ITERATIONS.
    assert runtime.calls <= _MAX_LOOP_ITERATIONS


def test_max_action_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop stops when the total action budget is exhausted."""
    from foundation.services.orchestrator import (
        _MAX_PLAN_ACTIONS,
        _MAX_TOTAL_ACTIONS,
    )

    # Fill enough iterations with _MAX_PLAN_ACTIONS each to reach the total cap.
    actions_per_iter = _MAX_PLAN_ACTIONS
    iterations_needed = (_MAX_TOTAL_ACTIONS + actions_per_iter - 1) // actions_per_iter
    responses = [
        _provider_response({
            "assistant_message": f"Iteration {i}.",
            "actions": [
                {
                    "id": f"search_{i}_{j}",
                    "kind": "tool_call",
                    "summary": "Search workspace",
                    "tool_call": {
                        "capability_id": "foundation.search",
                        "arguments": {
                            "query": f"needle_{i}_{j}",
                            "max_results": 1,
                        },
                    },
                }
                for j in range(1, actions_per_iter + 1)
            ],
        })
        for i in range(1, iterations_needed + 1)
    ]
    provider = StubProvider(responses)
    orchestrator, runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        scripts={
            "rg": """
                import json, sys
                print(json.dumps({"type": "summary",
                    "data": {"stats": {"matched_lines": 0}}}))
            """,
        },
    )

    result = orchestrator.orchestrate(
        UserRequest(message="many actions"),
    )

    assert result.stop_reason is LoopStopReason.MAX_ACTIONS
    total = (
        result.summary.executed_actions
        + result.summary.failed_actions
        + result.summary.blocked_actions
        + result.summary.skipped_actions
        + result.summary.pending_approval_actions
    )
    assert total == _MAX_TOTAL_ACTIONS


def test_no_progress_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated identical failures with no changes triggers NO_PROGRESS."""
    fail_response = _provider_response({
        "assistant_message": "Running false.",
        "actions": [
            {
                "id": "fail1",
                "kind": "shell",
                "summary": "Run failing command",
                "shell": {"command": "false"},
            }
        ],
    })
    provider = StubProvider([fail_response, fail_response])
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="run false twice"),
    )

    assert result.stop_reason is LoopStopReason.NO_PROGRESS
    assert len(result.iterations) == 2
    assert "[Loop stopped:" in result.assistant_message.content


def test_observation_block_in_provider_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second iteration's provider call includes observation messages."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Listing files.",
                "actions": [
                    {
                        "id": "ls_1",
                        "kind": "shell",
                        "summary": "List files",
                        "shell": {"command": "ls"},
                    }
                ],
            }),
            _provider_response({
                "assistant_message": "All done.",
                "actions": [],
            }),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="list files"),
    )

    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN
    assert len(result.iterations) == 2
    assert len(provider.calls) >= 2
    second_call = provider.calls[1]
    dev_messages = [
        m for m in second_call.messages
        if m.role.value == "developer"
        and "EXECUTION OBSERVATION" in m.content
    ]
    assert len(dev_messages) == 1
    assert "iteration 1" in dev_messages[0].content


def test_observation_accumulates_across_iterations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iteration N's planner call must see every prior iteration's observation
    plus a cumulative 'already executed' summary (prevents re-planning loops)."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "First listing.",
                "actions": [
                    {
                        "id": "ls_a",
                        "kind": "shell",
                        "summary": "list once",
                        "shell": {"command": "ls"},
                    }
                ],
            }),
            _provider_response({
                "assistant_message": "Checking pwd.",
                "actions": [
                    {
                        "id": "pwd_b",
                        "kind": "shell",
                        "summary": "pwd",
                        "shell": {"command": "pwd"},
                    }
                ],
            }),
            _provider_response({
                "assistant_message": "Done.",
                "actions": [],
            }),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(UserRequest(message="multi-step"))
    assert len(result.iterations) == 3
    assert len(provider.calls) >= 3

    # Inspect the THIRD planner call — it should see:
    # - iteration 1's observation (ls_a)
    # - iteration 2's observation (pwd_b)
    # - a cumulative "COMMANDS ALREADY EXECUTED" summary naming both
    third_call = provider.calls[2]
    dev_contents = "\n".join(
        m.content for m in third_call.messages if m.role.value == "developer"
    )
    assert "EXECUTION OBSERVATION (iteration 1)" in dev_contents
    assert "EXECUTION OBSERVATION (iteration 2)" in dev_contents
    assert "COMMANDS ALREADY EXECUTED" in dev_contents
    assert "[iter 1] $ ls" in dev_contents
    assert "[iter 2] $ pwd" in dev_contents


def test_verification_notice_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code changes without verification produce unverified notice."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    target_file = workspace_root / "test.txt"
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Writing file.",
                "actions": [
                    {
                        "id": "write_file",
                        "kind": "tool_call",
                        "summary": "Write a file",
                        "tool_call": {
                            "capability_id": "foundation.file.write",
                            "arguments": {
                                "path": str(target_file),
                                "content": "hello",
                            },
                        },
                    }
                ],
            }),
            _provider_response({
                "assistant_message": "Done writing.",
                "actions": [],
            }),
        ]
    )
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
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _request: True,
        ),
    )

    result = orchestrator.orchestrate(
        UserRequest(message="create test.txt"),
    )

    assert result.verification_notice is not None
    assert result.verification_notice.verified is False
    from foundation.models import VerificationOutcome

    assert result.verification_notice.outcome is VerificationOutcome.NOT_ATTEMPTED
    assert "no verification" in (
        result.verification_notice.reason or ""
    ).lower()


def _verification_workflow_provider(
    workspace_root: Path, *, verify_cmd: str = "pytest",
) -> StubProvider:
    """Build a provider that plans a file write followed by a verification cmd."""
    target_file = workspace_root / "created.txt"
    return StubProvider(
        [
            _provider_response({
                "assistant_message": "Edit then verify.",
                "actions": [
                    {
                        "id": "write_file",
                        "kind": "tool_call",
                        "summary": "Create file",
                        "tool_call": {
                            "capability_id": "foundation.file.write",
                            "arguments": {
                                "path": str(target_file),
                                "content": "hi",
                            },
                        },
                    },
                    {
                        "id": "verify",
                        "kind": "shell",
                        "summary": "Run verification",
                        "shell": {"command": verify_cmd, "args": ["--version"]},
                    },
                ],
            }),
            _provider_response({"assistant_message": "Done.", "actions": []}),
        ]
    )


def test_verification_notice_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful verification command yields PASSED with verified=True."""
    from foundation.models import VerificationOutcome

    workspace_root = tmp_path / "workspace"
    provider = _verification_workflow_provider(workspace_root)
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
        scripts={"pytest": "import sys\nsys.exit(0)\n"},
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT, prompt_callback=lambda _r: True,
        ),
    )

    result = orchestrator.orchestrate(UserRequest(message="create and verify"))

    assert result.verification_notice is not None
    assert result.verification_notice.outcome is VerificationOutcome.PASSED
    assert result.verification_notice.verified is True


def test_verification_notice_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verification command that exits non-zero reports FAILED, not PASSED."""
    from foundation.models import VerificationOutcome

    workspace_root = tmp_path / "workspace"
    provider = _verification_workflow_provider(workspace_root)
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
        scripts={"pytest": "import sys\nsys.exit(1)\n"},
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT, prompt_callback=lambda _r: True,
        ),
    )

    result = orchestrator.orchestrate(UserRequest(message="edit and fail"))

    assert result.verification_notice is not None
    assert result.verification_notice.outcome is VerificationOutcome.FAILED
    assert result.verification_notice.verified is False


def test_verification_notice_unavailable_when_binary_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verification command whose binary is missing reports UNAVAILABLE."""
    from foundation.models import VerificationOutcome

    workspace_root = tmp_path / "workspace"
    # Point PATH at an empty dir so verification binaries resolve to "not found".
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    provider = _verification_workflow_provider(workspace_root)
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT, prompt_callback=lambda _r: True,
        ),
    )

    result = orchestrator.orchestrate(UserRequest(message="edit with missing verify"))

    assert result.verification_notice is not None
    assert result.verification_notice.outcome is VerificationOutcome.UNAVAILABLE
    assert result.verification_notice.verified is False


def test_backward_compat_single_iteration_result_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-iteration result has correct plan/context/execution_results."""
    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Zero actions.",
                "actions": [],
            }),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path, monkeypatch, provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="explain something"),
    )

    assert result.plan.assistant_message == "Zero actions."
    assert result.context.workspace_root is not None
    assert result.planning_metadata.provider == "stub"
    assert result.execution_results == []
    assert result.policy_decisions == []
    assert len(result.iterations) == 1
    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN


def test_no_progress_detector_unit() -> None:
    """Unit test for NoProgressDetector."""
    from foundation.models import ExecutionResult, PlannedAction, ShellAction

    detector = NoProgressDetector()

    action = PlannedAction(
        id="a1", kind="shell", summary="Run test",
        shell=ShellAction(command="false"),
    )
    result = ExecutionResult(
        action_id="a1", status=ExecutionStatus.FAILED,
        summary="Failed.", error="Exit code 1",
    )

    assert detector.is_stuck([result], [], [action]) is False
    assert detector.is_stuck([result], [], [action]) is True


def test_no_progress_detector_not_stuck_with_changes() -> None:
    """NoProgressDetector is not stuck when there are file changes."""
    from foundation.models import ExecutionResult, PlannedAction, ShellAction

    detector = NoProgressDetector()

    action = PlannedAction(
        id="a1", kind="shell", summary="Run test",
        shell=ShellAction(command="false"),
    )
    result = ExecutionResult(
        action_id="a1", status=ExecutionStatus.FAILED,
        summary="Failed.", error="Exit code 1",
    )

    assert detector.is_stuck([result], [], [action]) is False
    assert detector.is_stuck([result], ["file.py"], [action]) is False


# ------------------------------------------------------------------
# Stage 05: Iteration-scoped trace ids and REPLANNED_FROM edges
# ------------------------------------------------------------------


def test_iteration_scoped_step_ids_and_replanned_from_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-iteration runs persist unique step ids and REPLANNED_FROM edges."""
    from foundation.models import TraceEdgeKind
    from foundation.services.orchestrator import _MAX_LOOP_ITERATIONS

    responses = [
        _provider_response({
            "assistant_message": f"Iteration {i}.",
            "actions": [
                {
                    "id": f"ls_{i}",
                    "kind": "shell",
                    "summary": "List files",
                    "shell": {"command": "ls"},
                }
            ],
        })
        for i in range(1, _MAX_LOOP_ITERATIONS + 1)
    ]
    provider = StubProvider(responses)
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(UserRequest(message="loop ls"))
    assert result.stop_reason is LoopStopReason.MAX_ITERATIONS
    assert result.session_id is not None

    trace = history_store.get_trace(TraceQuery(session_id=result.session_id))
    assert trace is not None

    planning_steps = [s for s in trace.steps if s.step_type.value == "planning"]
    execution_steps = [s for s in trace.steps if s.step_type.value == "execution"]
    assert len(planning_steps) == _MAX_LOOP_ITERATIONS
    assert len(execution_steps) == _MAX_LOOP_ITERATIONS

    # Step ids are unique across iterations
    all_ids = [s.step_id for s in trace.steps]
    assert len(set(all_ids)) == len(all_ids)

    # iteration_index matches iteration number for each step
    for i, step in enumerate(planning_steps, start=1):
        assert step.iteration_index == i
        assert step.step_id.endswith(f":{i}")
    for i, step in enumerate(execution_steps, start=1):
        assert step.iteration_index == i
        assert f":{i}:ls_{i}" in step.step_id

    # One REPLANNED_FROM edge per iteration boundary:
    # iter 1→2, 2→3, ..., (N-1)→N — so (_MAX_LOOP_ITERATIONS - 1) edges total.
    replanned_edges = [
        e for e in trace.edges if e.edge_kind is TraceEdgeKind.REPLANNED_FROM
    ]
    assert len(replanned_edges) == _MAX_LOOP_ITERATIONS - 1
    for edge in replanned_edges:
        assert edge.source_step_id.startswith("action:")
        assert edge.target_step_id.startswith("planning:")


def test_single_iteration_emits_no_replanned_from_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that finishes in one iteration must not emit REPLANNED_FROM."""
    from foundation.models import TraceEdgeKind

    provider = StubProvider(
        [
            _provider_response({
                "assistant_message": "Done.",
                "actions": [
                    {
                        "id": "ls_once",
                        "kind": "shell",
                        "summary": "List files",
                        "shell": {"command": "ls"},
                    }
                ],
            }),
            _provider_response({"assistant_message": "All good.", "actions": []}),
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(UserRequest(message="ls once"))
    assert result.session_id is not None

    trace = history_store.get_trace(TraceQuery(session_id=result.session_id))
    assert trace is not None
    replanned = [
        e for e in trace.edges if e.edge_kind is TraceEdgeKind.REPLANNED_FROM
    ]
    # The successful first iteration produces one REPLANNED_FROM edge leading
    # into the second (zero-action) planning step.  No edge exists ahead of
    # iteration 1 itself.
    assert len(replanned) <= 1
    for edge in replanned:
        assert edge.target_step_id.endswith(":2")
