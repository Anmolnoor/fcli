from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from foundation.models import (
    ActionKind,
    ExecutionArtifactType,
    ExecutionStatus,
    ExecutionStep,
    GapOptionKind,
    LoopStopReason,
    PlanningStep,
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseFormat,
    ProviderResponseMetadata,
    QuestionAction,
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
            return _provider_response(
                {
                    "assistant_message": "Done.",
                    "actions": [],
                }
            )
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


def _text_response(body: str) -> ProviderResponse:
    return ProviderResponse(
        content=body,
        structured_output=None,
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
    question_callback: Any | None = None,
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
        question_callback=question_callback,
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


def test_orchestrator_rejects_gh_api_raw_flag_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Fetching README.",
                    "actions": [
                        {
                            "id": "fetch_readme",
                            "kind": "shell",
                            "summary": "Fetch README",
                            "shell": {
                                "command": "gh",
                                "args": [
                                    "api",
                                    "repos/anmolnoor/anmolnoor/readme",
                                    "--jq",
                                    ".content",
                                    "-r",
                                ],
                            },
                        }
                    ],
                }
            ),
            _provider_response({"assistant_message": "Corrected.", "actions": []}),
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="fetch my GitHub README"))

    assert runtime.calls == 0
    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN
    assert len(provider.calls) == 2
    repair_text = "\n".join(message.content for message in provider.calls[1].messages)
    assert "gh api" in repair_text
    assert "does not support `-r`" in repair_text


def test_orchestrator_recovers_from_truncated_plan_with_content_brief_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation.services.provider import ProviderError, ProviderErrorCode

    class _TruncateThenSucceed:
        def __init__(self) -> None:
            self.calls: list[ProviderPrompt] = []
            self._raised = False

        def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
            self.calls.append(prompt)
            if not self._raised:
                self._raised = True
                raise ProviderError(
                    "Provider response was truncated before completion (done_reason=length).",
                    code=ProviderErrorCode.TRUNCATED,
                    response_text='{"assistant_message":"writing","actions":[{"id":"w"',
                )
            return _provider_response({"assistant_message": "Done.", "actions": []})

    provider = _TruncateThenSucceed()
    orchestrator, runtime, _ = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="write a big file"))

    # Truncated attempt + repaired retry, both within iteration 1's planning.
    assert len(provider.calls) == 2
    assert runtime.calls == 0
    repair_text = "\n".join(m.content for m in provider.calls[1].messages)
    assert "truncated" in repair_text.lower()
    assert "content_brief" in repair_text
    # First attempt is deterministic; the repair retry nudges temperature off 0.
    assert provider.calls[0].temperature is None
    assert provider.calls[1].temperature == 0.4
    assert result.summary is not None


def test_orchestrator_materializes_content_brief_via_text_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Report\n\n" + ("This is a long generated body. " * 50)
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing the report.",
                    "actions": [
                        {
                            "id": "write_report",
                            "kind": "tool_call",
                            "summary": "Write the report",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {
                                    "path": "report.md",
                                    "content_brief": "a markdown report about the project",
                                },
                            },
                        }
                    ],
                }
            ),
            _text_response(body),
        ]
    )
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="write a report"))

    # The body was generated by a separate TEXT call, then written verbatim.
    assert (workspace_root / "report.md").read_text(encoding="utf-8") == body
    materialization_call = provider.calls[1]
    assert materialization_call.response_format is ProviderResponseFormat.TEXT
    # The plan call itself never carried the large body inline.
    plan_call_text = "\n".join(m.content for m in provider.calls[0].messages)
    assert body not in plan_call_text
    assert any(r.status is ExecutionStatus.EXECUTED for r in result.execution_results)


def test_orchestrator_recovers_file_write_note_as_content_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# README Summary\n\n- Foundation CLI\n- Beekeeper\n"
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing the summary.",
                    "actions": [
                        {
                            "id": "write_summary",
                            "kind": "tool_call",
                            "summary": "Write the summary",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {
                                    "path": "res/github-readme-summary.md",
                                    "overwrite": True,
                                },
                                "_file_write_note": (
                                    "content_brief: Markdown file containing extracted "
                                    "sections from the GitHub README"
                                ),
                            },
                        }
                    ],
                }
            ),
            _text_response(body),
        ]
    )
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="summarize my GitHub README"))

    assert (workspace_root / "res/github-readme-summary.md").read_text(encoding="utf-8") == body
    assert provider.calls[1].response_format is ProviderResponseFormat.TEXT
    assert any(r.status is ExecutionStatus.EXECUTED for r in result.execution_results)


def test_orchestrator_deferred_write_failure_degrades_to_failed_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation.services.provider import ProviderError, ProviderErrorCode

    plan = _provider_response(
        {
            "assistant_message": "Writing the report.",
            "actions": [
                {
                    "id": "write_report",
                    "kind": "tool_call",
                    "summary": "Write the report",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {
                            "path": "report.md",
                            "content_brief": "a markdown report",
                        },
                    },
                }
            ],
        }
    )

    class _PlanThenFailBody:
        def __init__(self) -> None:
            self.calls: list[ProviderPrompt] = []

        def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
            self.calls.append(prompt)
            if len(self.calls) == 1:
                return plan
            if len(self.calls) == 2:
                raise ProviderError("body truncated", code=ProviderErrorCode.TRUNCATED)
            return _provider_response({"assistant_message": "Done.", "actions": []})

    provider = _PlanThenFailBody()
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="write a report"))

    # Body generation failed -> the write degrades to a failed action, no file written.
    assert not (workspace_root / "report.md").exists()
    assert any(r.status is ExecutionStatus.FAILED for r in result.execution_results)


def test_orchestrator_question_answer_flows_to_next_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I need to clarify the format first.",
                    "actions": [
                        {
                            "id": "ask_format",
                            "kind": "question",
                            "summary": "Ask which output format",
                            "question": {
                                "prompt": "Which format?",
                                "options": ["json", "yaml"],
                            },
                        }
                    ],
                }
            )
        ]
    )
    asked: list[str] = []

    def callback(question: QuestionAction) -> str:
        asked.append(question.prompt)
        return "json"

    orchestrator, _, _ = _orchestrator(tmp_path, monkeypatch, provider, question_callback=callback)

    result = orchestrator.orchestrate(UserRequest(message="export the data"))

    assert asked == ["Which format?"]
    assert any(r.status is ExecutionStatus.EXECUTED for r in result.execution_results)
    # Iteration 2's plan request carried the answer back to the planner.
    second_plan_text = "\n".join(m.content for m in provider.calls[1].messages)
    assert "User answered" in second_plan_text
    assert "json" in second_plan_text


def test_orchestrator_question_without_callback_stops_awaiting_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I need to clarify the format first.",
                    "actions": [
                        {
                            "id": "ask_format",
                            "kind": "question",
                            "summary": "Ask which output format",
                            "question": {"prompt": "Which format?"},
                        }
                    ],
                }
            )
        ]
    )
    # No question_callback => non-interactive.
    orchestrator, _, _ = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="export the data"))

    assert result.stop_reason is LoopStopReason.AWAITING_USER_INPUT
    assert any(r.status is ExecutionStatus.AWAITING_INPUT for r in result.execution_results)


def _read_action(action_id: str, path: str) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "tool_call",
        "summary": f"Read {path}",
        "tool_call": {"capability_id": "foundation.file.read", "arguments": {"path": path}},
    }


def test_orchestrator_out_of_scope_read_escalation_grants_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("top secret\n", encoding="utf-8")
    sibling = outside / "other.md"
    sibling.write_text("also here\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Reading the external file.",
                    "actions": [_read_action("read_secret", str(secret))],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Reading a sibling under the same root.",
                    "actions": [_read_action("read_sibling", str(sibling))],
                }
            ),
        ]
    )
    prompts: list[str] = []

    def callback(question: QuestionAction) -> str:
        prompts.append(question.prompt)
        return "Allow for this session"

    orchestrator, _, _ = _orchestrator(tmp_path, monkeypatch, provider, question_callback=callback)

    result = orchestrator.orchestrate(UserRequest(message="read the external secret"))

    # Prompted exactly once; the sibling read under the granted root did not re-prompt.
    assert len(prompts) == 1
    executed_reads = [
        r
        for r in result.execution_results
        if r.artifact_type is ExecutionArtifactType.FILE_READ
        and r.status is ExecutionStatus.EXECUTED
    ]
    assert len(executed_reads) == 2
    assert any(r.artifact and r.artifact.get("content") == "top secret\n" for r in executed_reads)


def test_orchestrator_out_of_scope_read_escalation_deny_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("top secret\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Reading the external file.",
                    "actions": [_read_action("read_secret", str(secret))],
                }
            )
        ]
    )

    def callback(_question: QuestionAction) -> str:
        return "Deny"

    orchestrator, _, _ = _orchestrator(tmp_path, monkeypatch, provider, question_callback=callback)

    result = orchestrator.orchestrate(UserRequest(message="read the external secret"))

    assert any(r.status is ExecutionStatus.BLOCKED for r in result.execution_results)
    assert not any(
        r.artifact_type is ExecutionArtifactType.FILE_READ and r.status is ExecutionStatus.EXECUTED
        for r in result.execution_results
    )


def test_orchestrator_out_of_scope_write_stays_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "evil.md"

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing outside the workspace.",
                    "actions": [
                        {
                            "id": "write_evil",
                            "kind": "tool_call",
                            "summary": "Write outside the workspace",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {"path": str(target), "content": "x"},
                            },
                        }
                    ],
                }
            )
        ]
    )
    prompts: list[str] = []

    def callback(question: QuestionAction) -> str:
        prompts.append(question.prompt)
        return "Allow for this session"

    orchestrator, _, _ = _orchestrator(tmp_path, monkeypatch, provider, question_callback=callback)

    result = orchestrator.orchestrate(UserRequest(message="write outside"))

    # Writes are never escalated: no prompt, blocked, nothing written.
    assert prompts == []
    assert any(r.status is ExecutionStatus.BLOCKED for r in result.execution_results)
    assert not target.exists()


def test_orchestrator_retries_shell_cat_plan_without_executing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "I'll read the file with cat.",
                    "actions": [
                        {
                            "id": "read_with_cat",
                            "kind": "shell",
                            "summary": "Read the note with cat",
                            "shell": {
                                "command": "cat",
                                "args": ["note.txt"],
                            },
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "I'll use the typed file reader instead.",
                    "actions": [
                        {
                            "id": "read_note",
                            "kind": "tool_call",
                            "summary": "Read the note with the file capability",
                            "tool_call": {
                                "capability_id": "foundation.file.read",
                                "arguments": {"path": "note.txt"},
                            },
                        }
                    ],
                }
            ),
        ]
    )
    orchestrator, runtime, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)
    (workspace_root / "note.txt").write_text("hello\n", encoding="utf-8")

    result = orchestrator.orchestrate(UserRequest(message="read note.txt"))

    assert len(provider.calls) == 3
    assert runtime.calls == 0
    assert result.execution_results[0].status is ExecutionStatus.EXECUTED
    assert result.execution_results[0].artifact is not None
    assert Path(str(result.execution_results[0].artifact["path"])).name == "note.txt"


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
                                # `head` keeps the test focused on the
                                # workspace-confinement policy check; `cat`
                                # is now planner-rejected as a typed-capability
                                # equivalent.
                                "command": "head",
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
    assert isinstance(planning_step, PlanningStep)
    assert isinstance(execution_step, ExecutionStep)
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

    trace = history_store.get_trace(TraceQuery(session_id=result.session_id or ""))
    assert trace is not None
    assert len(trace.steps) == 2
    execution_step = next(s for s in trace.steps if isinstance(s, ExecutionStep))
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
    # The loop recovered to a zero-action final plan, so the session is
    # recorded as COMPLETED even though a mid-iteration action failed.
    # The per-action failure is preserved in execution_results/summary.
    assert sessions[0].status is SessionStatus.COMPLETED

    full_trace = history_store.get_trace(TraceQuery(session_id=result.session_id or ""))
    assert full_trace is not None
    execution_step = next(s for s in full_trace.steps if isinstance(s, ExecutionStep))
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
            _provider_response(
                {
                    "assistant_message": "The answer is 42.",
                    "actions": [],
                }
            ),
        ]
    )
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
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
            _provider_response(
                {
                    "assistant_message": "Need to delete that file.",
                    "actions": [
                        {
                            "id": "rm_file",
                            "kind": "shell",
                            "summary": "Remove a file",
                            "shell": {"command": "rm", "args": ["x.txt"]},
                        }
                    ],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
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
            _provider_response(
                {
                    "assistant_message": "Running nonexistent.",
                    "actions": [
                        {
                            "id": "spawn_fail",
                            "kind": "shell",
                            "summary": "Run a nonexistent binary",
                            "shell": {"command": "nonexistent_binary_xyz"},
                        }
                    ],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="run nonexistent"),
    )

    assert result.stop_reason is LoopStopReason.FATAL_EXECUTION_FAILURE
    assert len(result.iterations) == 1
    assert result.summary.failed_actions == 1


def test_fatal_failure_reframed_as_capability_gap_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fatal stop surfaces a graceful handoff instead of a raw error suffix."""
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Running nonexistent.",
                    "actions": [
                        {
                            "id": "spawn_fail",
                            "kind": "shell",
                            "summary": "Run a nonexistent binary",
                            "shell": {"command": "nonexistent_binary_xyz"},
                        }
                    ],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="run nonexistent"))

    assert result.stop_reason is LoopStopReason.FATAL_EXECUTION_FAILURE
    # The chat surface shows the handoff message, not the red "[Loop stopped...]" suffix.
    assert result.gap_handoff is not None
    assert result.assistant_message.content == result.gap_handoff.message
    assert "[Loop stopped" not in result.assistant_message.content
    # The underlying failure is preserved for logs/trace (hidden from chat, not deleted).
    assert any(r.status is ExecutionStatus.FAILED for r in result.execution_results)
    option_kinds = {option.kind for option in result.gap_handoff.options}
    assert GapOptionKind.REPORT in option_kinds
    assert GapOptionKind.STOP in option_kinds


def test_capability_gap_message_is_model_phrased(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the fatal plan, the provider's text reply phrases the gap message."""
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Running nonexistent.",
                    "actions": [
                        {
                            "id": "spawn_fail",
                            "kind": "shell",
                            "summary": "Run a nonexistent binary",
                            "shell": {"command": "nonexistent_binary_xyz"},
                        }
                    ],
                }
            ),
            _text_response("That tool isn't wired into fcli yet, so I couldn't run it."),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    result = orchestrator.orchestrate(UserRequest(message="run nonexistent"))

    assert result.gap_handoff is not None
    assert result.gap_handoff.message == (
        "That tool isn't wired into fcli yet, so I couldn't run it."
    )
    assert result.assistant_message.content == result.gap_handoff.message


def test_max_iteration_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop stops at the configured max iteration cap."""
    from foundation.services.orchestrator import _MAX_LOOP_ITERATIONS

    responses = [
        _provider_response(
            {
                "assistant_message": f"Iteration {i} running ls.",
                "actions": [
                    {
                        "id": f"ls_{i}",
                        "kind": "shell",
                        "summary": "List files",
                        "shell": {"command": "ls"},
                    }
                ],
            }
        )
        for i in range(1, _MAX_LOOP_ITERATIONS + 1)
    ]
    provider = StubProvider(responses)
    orchestrator, runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
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
        _provider_response(
            {
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
            }
        )
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
    fail_response = _provider_response(
        {
            "assistant_message": "Running false.",
            "actions": [
                {
                    "id": "fail1",
                    "kind": "shell",
                    "summary": "Run failing command",
                    "shell": {"command": "false"},
                }
            ],
        }
    )
    provider = StubProvider([fail_response, fail_response])
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="run false twice"),
    )

    assert result.stop_reason is LoopStopReason.NO_PROGRESS
    assert len(result.iterations) == 2
    # A stuck loop with no cumulative changes is reframed as a capability-gap
    # handoff, so the user sees a graceful message rather than the raw suffix.
    assert result.gap_handoff is not None
    assert "[Loop stopped:" not in result.assistant_message.content


def test_command_usage_error_adds_repair_instruction_to_next_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Fetching README.",
                    "actions": [
                        {
                            "id": "readme",
                            "kind": "shell",
                            "summary": "Fetch README",
                            "shell": {
                                "command": "git",
                                "args": [
                                    "status",
                                    "-r",
                                ],
                            },
                        }
                    ],
                }
            ),
            _provider_response({"assistant_message": "Stopped.", "actions": []}),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        scripts={
            "git": """
                import sys
                print("unknown shorthand flag: 'r' in -r", file=sys.stderr)
                print("Usage: git status [options]", file=sys.stderr)
                raise SystemExit(1)
            """,
        },
    )

    orchestrator.orchestrate(UserRequest(message="fetch the README"))

    second_prompt = "\n".join(message.content for message in provider.calls[1].messages)
    assert "previous command failed because its argv is invalid" in second_prompt
    assert "Do not repeat it" in second_prompt
    assert "git status -r" in second_prompt
    assert "unknown shorthand flag: 'r' in -r" in second_prompt


def test_repeated_command_usage_error_preserves_stderr_in_final_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_response = _provider_response(
        {
            "assistant_message": "Trying git status.",
            "actions": [
                {
                    "id": "bad_status",
                    "kind": "shell",
                    "summary": "Run git status",
                    "shell": {"command": "git", "args": ["status", "-r"]},
                }
            ],
        }
    )
    provider = StubProvider([fail_response, fail_response])
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        scripts={
            "git": """
                import sys
                print("unknown shorthand flag: 'r' in -r", file=sys.stderr)
                print("Usage: git status [options]", file=sys.stderr)
                raise SystemExit(1)
            """,
        },
    )

    result = orchestrator.orchestrate(UserRequest(message="run git status"))

    assert result.stop_reason is LoopStopReason.NO_PROGRESS
    assert result.gap_handoff is None
    assert "command invocation error" in result.assistant_message.content
    assert "git status -r" in result.assistant_message.content
    assert "unknown shorthand flag: 'r' in -r" in result.assistant_message.content
    assert "capability" not in result.assistant_message.content.lower()


def test_recovered_command_usage_error_does_not_pollute_success_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Trying git status.",
                    "actions": [
                        {
                            "id": "bad_status",
                            "kind": "shell",
                            "summary": "Run git status",
                            "shell": {"command": "git", "args": ["status", "-r"]},
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Writing the result.",
                    "actions": [
                        {
                            "id": "write_result",
                            "kind": "tool_call",
                            "summary": "Write the result",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {
                                    "path": "result.txt",
                                    "content": "done\n",
                                    "overwrite": True,
                                },
                            },
                        }
                    ],
                }
            ),
            _provider_response({"assistant_message": "Done.", "actions": []}),
        ]
    )
    orchestrator, _runtime, workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        scripts={
            "git": """
                import sys
                print("unknown shorthand flag: 'r' in -r", file=sys.stderr)
                print("Usage: git status [options]", file=sys.stderr)
                raise SystemExit(1)
            """,
        },
    )

    result = orchestrator.orchestrate(UserRequest(message="recover and finish"))

    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN
    assert (workspace_root / "result.txt").read_text(encoding="utf-8") == "done\n"
    assert result.assistant_message.content == "Done."
    assert "command invocation error" not in result.assistant_message.content
    assert "unknown shorthand flag" not in result.assistant_message.content


def test_observation_block_in_provider_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second iteration's provider call includes observation messages."""
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Listing files.",
                    "actions": [
                        {
                            "id": "ls_1",
                            "kind": "shell",
                            "summary": "List files",
                            "shell": {"command": "ls"},
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "All done.",
                    "actions": [],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="list files"),
    )

    assert result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN
    assert len(result.iterations) == 2
    assert len(provider.calls) >= 2
    second_call = provider.calls[1]
    dev_messages = [
        m
        for m in second_call.messages
        if m.role.value == "developer" and "EXECUTION OBSERVATION" in m.content
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
            _provider_response(
                {
                    "assistant_message": "First listing.",
                    "actions": [
                        {
                            "id": "ls_a",
                            "kind": "shell",
                            "summary": "list once",
                            "shell": {"command": "ls"},
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Checking pwd.",
                    "actions": [
                        {
                            "id": "pwd_b",
                            "kind": "shell",
                            "summary": "pwd",
                            "shell": {"command": "pwd"},
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Done.",
                    "actions": [],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
    )

    result = orchestrator.orchestrate(UserRequest(message="multi-step"))
    assert len(result.iterations) == 3
    assert len(provider.calls) >= 3

    # Inspect the THIRD planner call — it should see:
    # - iteration 1's observation (ls_a)
    # - iteration 2's observation (pwd_b)
    # - a cumulative "COMMANDS ALREADY EXECUTED" summary naming both
    third_call = provider.calls[2]
    dev_contents = "\n".join(m.content for m in third_call.messages if m.role.value == "developer")
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
            _provider_response(
                {
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
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Done writing.",
                    "actions": [],
                }
            ),
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
    assert "no verification" in (result.verification_notice.reason or "").lower()


def _verification_workflow_provider(
    workspace_root: Path,
    *,
    verify_cmd: str = "pytest",
) -> StubProvider:
    """Build a provider that plans a file write followed by a verification cmd."""
    target_file = workspace_root / "created.txt"
    return StubProvider(
        [
            _provider_response(
                {
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
                }
            ),
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
        tmp_path,
        monkeypatch,
        provider,
        scripts={"pytest": "import sys\nsys.exit(0)\n"},
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _r: True,
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
        tmp_path,
        monkeypatch,
        provider,
        scripts={"pytest": "import sys\nsys.exit(1)\n"},
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _r: True,
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
        tmp_path,
        monkeypatch,
        provider,
        approval_service=ApprovalService(
            mode=ApprovalMode.PROMPT,
            prompt_callback=lambda _r: True,
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
            _provider_response(
                {
                    "assistant_message": "Zero actions.",
                    "actions": [],
                }
            ),
        ]
    )
    orchestrator, _runtime, _workspace_root = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
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
        id="a1",
        kind=ActionKind.SHELL,
        summary="Run test",
        shell=ShellAction(command="false"),
    )
    result = ExecutionResult(
        action_id="a1",
        status=ExecutionStatus.FAILED,
        summary="Failed.",
        error="Exit code 1",
    )

    assert detector.is_stuck([result], [], [action]) is False
    assert detector.is_stuck([result], [], [action]) is True


def test_no_progress_detector_not_stuck_with_changes() -> None:
    """NoProgressDetector is not stuck when there are file changes."""
    from foundation.models import ExecutionResult, PlannedAction, ShellAction

    detector = NoProgressDetector()

    action = PlannedAction(
        id="a1",
        kind=ActionKind.SHELL,
        summary="Run test",
        shell=ShellAction(command="false"),
    )
    result = ExecutionResult(
        action_id="a1",
        status=ExecutionStatus.FAILED,
        summary="Failed.",
        error="Exit code 1",
    )

    assert detector.is_stuck([result], [], [action]) is False
    assert detector.is_stuck([result], ["file.py"], [action]) is False


# ------------------------------------------------------------------
# v4 Stage 03: detector + observation + status mapping
# ------------------------------------------------------------------


def test_detector_window_two_requires_two_consecutive_repeats() -> None:
    """A single repeat does not trip stuck; two in a row does."""
    from foundation.models import ExecutionResult, PlannedAction, ShellAction

    detector = NoProgressDetector()
    action = PlannedAction(
        id="a",
        kind=ActionKind.SHELL,
        summary="run",
        shell=ShellAction(command="false"),
    )
    fail = ExecutionResult(
        action_id="a",
        status=ExecutionStatus.FAILED,
        summary="boom",
        error="Exit code 1",
    )
    # iter 1: first failure observed; window not yet full → not stuck.
    assert detector.is_stuck([fail], [], [action]) is False
    # iter 2: second identical failure → window=2 satisfied → stuck.
    assert detector.is_stuck([fail], [], [action]) is True


def test_detector_cumulative_changes_suppresses_stuck() -> None:
    """Earlier-iteration progress should never declare the loop stuck."""
    from foundation.models import ExecutionResult, PlannedAction, ShellAction

    detector = NoProgressDetector()
    action = PlannedAction(
        id="a",
        kind=ActionKind.SHELL,
        summary="run",
        shell=ShellAction(command="false"),
    )
    fail = ExecutionResult(
        action_id="a",
        status=ExecutionStatus.FAILED,
        summary="boom",
        error="Exit code 1",
    )
    detector.is_stuck([fail], [], [action])
    # Even with the second identical failure, cumulative changes prevent stuck.
    assert (
        detector.is_stuck(
            [fail],
            [],
            [action],
            cumulative_changed_paths=["edited.py"],
        )
        is False
    )


def test_filter_results_for_detector_demotes_file_exists_after_prior_write() -> None:
    """A FILE_EXISTS error on a path already in the cumulative set is soft."""
    from foundation.models import ExecutionResult, PlannedAction, ToolCall
    from foundation.services.orchestrator import _filter_results_for_detector

    write = PlannedAction(
        id="w1",
        kind=ActionKind.TOOL_CALL,
        summary="rewrite",
        tool_call=ToolCall(
            capability_id="foundation.file.write",
            arguments={"path": "/abs/notes.md", "content": "x"},
        ),
    )
    failed = ExecutionResult(
        action_id="w1",
        status=ExecutionStatus.FAILED,
        summary="exists",
        error="File already exists. Set overwrite=true to replace it.",
    )
    filtered = _filter_results_for_detector(
        [failed],
        [write],
        cumulative_changed_paths={"/abs/notes.md"},
    )
    assert filtered[0].status is ExecutionStatus.NOT_EXECUTED


def test_filter_results_for_detector_keeps_real_failures() -> None:
    """An error on a path *not* yet in cumulative changes stays a failure."""
    from foundation.models import ExecutionResult, PlannedAction, ToolCall
    from foundation.services.orchestrator import _filter_results_for_detector

    write = PlannedAction(
        id="w1",
        kind=ActionKind.TOOL_CALL,
        summary="rewrite",
        tool_call=ToolCall(
            capability_id="foundation.file.write",
            arguments={"path": "/abs/notes.md"},
        ),
    )
    failed = ExecutionResult(
        action_id="w1",
        status=ExecutionStatus.FAILED,
        summary="exists",
        error="File already exists. Set overwrite=true to replace it.",
    )
    filtered = _filter_results_for_detector(
        [failed],
        [write],
        cumulative_changed_paths=set(),
    )
    assert filtered[0].status is ExecutionStatus.FAILED


def test_filter_results_for_detector_demotes_probe_reads() -> None:
    """A failed file.read whose target is also written this iteration → soft."""
    from foundation.models import ExecutionResult, PlannedAction, ToolCall
    from foundation.services.orchestrator import _filter_results_for_detector

    probe = PlannedAction(
        id="r1",
        kind=ActionKind.TOOL_CALL,
        summary="probe",
        tool_call=ToolCall(
            capability_id="foundation.file.read",
            arguments={"path": "/abs/new.md"},
        ),
    )
    write = PlannedAction(
        id="w1",
        kind=ActionKind.TOOL_CALL,
        summary="write",
        tool_call=ToolCall(
            capability_id="foundation.file.write",
            arguments={"path": "/abs/new.md", "content": "x"},
        ),
    )
    probe_failed = ExecutionResult(
        action_id="r1",
        status=ExecutionStatus.FAILED,
        summary="missing",
        error="File not found.",
    )
    write_ok = ExecutionResult(
        action_id="w1",
        status=ExecutionStatus.EXECUTED,
        summary="ok",
    )
    filtered = _filter_results_for_detector(
        [probe_failed, write_ok],
        [probe, write],
        cumulative_changed_paths=set(),
    )
    assert filtered[0].status is ExecutionStatus.NOT_EXECUTED
    assert filtered[1].status is ExecutionStatus.EXECUTED


def test_format_tool_call_log_entry_renders_path_and_message() -> None:
    """The tool-call summary line is stable for the planner's history."""
    from foundation.models import ToolCall
    from foundation.services.orchestrator import _format_tool_call_log_entry

    write_call = ToolCall(
        capability_id="foundation.file.write",
        arguments={"path": "/abs/notes.md", "content": "ignored"},
    )
    assert _format_tool_call_log_entry(write_call) == (
        "tool_call:foundation.file.write path=/abs/notes.md"
    )
    commit_call = ToolCall(
        capability_id="foundation.git.commit",
        arguments={"message": "tighten greeting"},
    )
    assert _format_tool_call_log_entry(commit_call) == (
        "tool_call:foundation.git.commit message=tighten greeting"
    )


def test_session_status_for_no_progress_with_cumulative_changes_is_completed() -> None:
    """Soft completion: NO_PROGRESS + cumulative changes + no fatal → COMPLETED."""
    from foundation.models import (
        LoopStopReason,
        OrchestrationSummary,
        SessionStatus,
    )

    summary = OrchestrationSummary(
        text="ran",
        executed_actions=2,
        pending_approval_actions=0,
        blocked_actions=0,
        failed_actions=0,
        skipped_actions=0,
        total_iterations=2,
        total_actions_planned=2,
    )
    status = RequestOrchestrator._session_status_for_result(
        summary,
        LoopStopReason.NO_PROGRESS,
        iterations=[],
        cumulative_changed_paths=["/abs/notes.md"],
        had_fatal=False,
    )
    assert status is SessionStatus.COMPLETED


def test_session_status_for_no_progress_without_changes_is_inconclusive() -> None:
    """NO_PROGRESS with no cumulative changes → COMPLETED_INCONCLUSIVE."""
    from foundation.models import (
        LoopStopReason,
        OrchestrationSummary,
        SessionStatus,
    )

    summary = OrchestrationSummary(
        text="ran",
        executed_actions=0,
        pending_approval_actions=0,
        blocked_actions=0,
        failed_actions=2,
        skipped_actions=0,
        total_iterations=2,
        total_actions_planned=2,
    )
    status = RequestOrchestrator._session_status_for_result(
        summary,
        LoopStopReason.NO_PROGRESS,
        iterations=[],
        cumulative_changed_paths=[],
        had_fatal=False,
    )
    assert status is SessionStatus.COMPLETED_INCONCLUSIVE


def test_orchestrator_soft_completion_replays_reference_incident(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference incident shape: write succeeds, idempotent re-write fails,
    detector tolerates one repeat, second repeat → NO_PROGRESS but the
    workspace already has the change, so status is COMPLETED."""
    from foundation.models import (
        LoopStopReason,
        SessionStatus,
    )

    # Workspace is created by _orchestrator(); we just record the future
    # target path so the stub responses can reference it.
    target = tmp_path / "workspace" / "notes.md"

    # iter 1: probe (read fails because file doesn't exist).
    iter1_response = _provider_response(
        {
            "assistant_message": "Probing file.",
            "actions": [
                {
                    "id": "probe",
                    "kind": "tool_call",
                    "summary": "Read existing file",
                    "tool_call": {
                        "capability_id": "foundation.file.read",
                        "arguments": {"path": str(target)},
                    },
                },
            ],
        }
    )
    # iter 2: write succeeds.
    iter2_response = _provider_response(
        {
            "assistant_message": "Writing.",
            "actions": [
                {
                    "id": "write1",
                    "kind": "tool_call",
                    "summary": "Write file",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {
                            "path": str(target),
                            "content": "hello\n",
                        },
                    },
                },
            ],
        }
    )
    # iter 3: planner re-issues the write. FILE_EXISTS error.
    iter3_response = _provider_response(
        {
            "assistant_message": "Writing again.",
            "actions": [
                {
                    "id": "write2",
                    "kind": "tool_call",
                    "summary": "Write file",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {
                            "path": str(target),
                            "content": "hello\n",
                        },
                    },
                },
            ],
        }
    )
    # iter 4+: same idempotent re-issue — detector trips (window=2).
    iter4_response = _provider_response(
        {
            "assistant_message": "Writing again.",
            "actions": [
                {
                    "id": "write3",
                    "kind": "tool_call",
                    "summary": "Write file",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {
                            "path": str(target),
                            "content": "hello\n",
                        },
                    },
                },
            ],
        }
    )
    provider = StubProvider([iter1_response, iter2_response, iter3_response, iter4_response])
    orchestrator, _runtime, ws = _orchestrator(
        tmp_path,
        monkeypatch,
        provider,
        approval_service=ApprovalService(mode=ApprovalMode.AUTO),
    )
    # _orchestrator created its own workspace; redirect the target into it.
    target = ws / "notes.md"
    for response in [iter1_response, iter2_response, iter3_response, iter4_response]:
        for action in response.structured_output["actions"]:
            action["tool_call"]["arguments"]["path"] = str(target)

    result = orchestrator.orchestrate(UserRequest(message="rewrite the notes"))

    # Workspace state reflects the user's intent.
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "hello\n"

    # The loop stops cleanly. Either NO_PROGRESS (detector tripped) or
    # ZERO_ACTION_PLAN (provider exhausted) is acceptable per stage 03.
    assert result.stop_reason in (
        LoopStopReason.NO_PROGRESS,
        LoopStopReason.ZERO_ACTION_PLAN,
    )

    # Status is COMPLETED, not COMPLETED_INCONCLUSIVE.
    summary_status = RequestOrchestrator._session_status_for_result(
        result.summary,
        result.stop_reason,
        result.iterations,
        result.governance_notice,
    )
    assert summary_status is SessionStatus.COMPLETED

    # The soft-completion notice replaces the red "no progress" suffix.
    if result.stop_reason is LoopStopReason.NO_PROGRESS:
        assert "Run complete" in result.assistant_message.content
        assert "no progress detected" not in result.assistant_message.content


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
        _provider_response(
            {
                "assistant_message": f"Iteration {i}.",
                "actions": [
                    {
                        "id": f"ls_{i}",
                        "kind": "shell",
                        "summary": "List files",
                        "shell": {"command": "ls"},
                    }
                ],
            }
        )
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
    replanned_edges = [e for e in trace.edges if e.edge_kind is TraceEdgeKind.REPLANNED_FROM]
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
            _provider_response(
                {
                    "assistant_message": "Done.",
                    "actions": [
                        {
                            "id": "ls_once",
                            "kind": "shell",
                            "summary": "List files",
                            "shell": {"command": "ls"},
                        }
                    ],
                }
            ),
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
    replanned = [e for e in trace.edges if e.edge_kind is TraceEdgeKind.REPLANNED_FROM]
    # The successful first iteration produces one REPLANNED_FROM edge leading
    # into the second (zero-action) planning step.  No edge exists ahead of
    # iteration 1 itself.
    assert len(replanned) <= 1
    for edge in replanned:
        assert edge.target_step_id.endswith(":2")


# ---------------------------------------------------------------------------
# Session-status classification (new in the v3-qa-tidy PR series)
# ---------------------------------------------------------------------------


def _status_for(
    stop_reason: LoopStopReason | None,
    *,
    executed: int = 0,
    pending: int = 0,
    failed: int = 0,
    blocked: int = 0,
    skipped: int = 0,
    iterations: list | None = None,
) -> SessionStatus:
    from foundation.models import OrchestrationSummary

    summary = OrchestrationSummary(
        executed_actions=executed,
        pending_approval_actions=pending,
        blocked_actions=blocked,
        failed_actions=failed,
        skipped_actions=skipped,
        total_iterations=1,
        total_actions_planned=executed + pending + failed + blocked + skipped,
        text="test",
    )
    return RequestOrchestrator._session_status_for_result(
        summary,
        stop_reason,
        iterations or [],
    )


def test_session_status_pending_approval_takes_precedence() -> None:
    assert (
        _status_for(
            LoopStopReason.ZERO_ACTION_PLAN,
            executed=2,
            pending=1,
        )
        is SessionStatus.PENDING_APPROVAL
    )


def test_session_status_fatal_execution_failure_is_failed() -> None:
    assert (
        _status_for(
            LoopStopReason.FATAL_EXECUTION_FAILURE,
            failed=1,
        )
        is SessionStatus.FAILED
    )


def test_session_status_zero_action_plan_with_intermediate_failure_is_completed() -> None:
    # This is the recovery case: earlier iteration had a failure, the final
    # iteration returned zero actions cleanly. The run is recorded as COMPLETED.
    assert (
        _status_for(
            LoopStopReason.ZERO_ACTION_PLAN,
            executed=1,
            failed=1,
        )
        is SessionStatus.COMPLETED
    )


def test_session_status_zero_action_plan_clean_is_completed() -> None:
    assert (
        _status_for(
            LoopStopReason.ZERO_ACTION_PLAN,
            executed=2,
        )
        is SessionStatus.COMPLETED
    )


def test_session_status_no_progress_is_inconclusive() -> None:
    # Loop terminated because the planner kept making the same mistakes.
    # The assistant delivered a final message; the run is not corrupt but
    # also didn't satisfy the full request.
    assert (
        _status_for(
            LoopStopReason.NO_PROGRESS,
            executed=1,
            failed=2,
        )
        is SessionStatus.COMPLETED_INCONCLUSIVE
    )


def test_session_status_max_iterations_with_clean_final_is_inconclusive() -> None:
    from foundation.models import (
        AssistantPlan,
        ContextSnapshot,
        OrchestrationIteration,
        ProviderResponseMetadata,
    )

    context = ContextSnapshot(
        workspace_root="/tmp/w",
        request_cwd="/tmp/w",
        approval_mode="prompt",
    )
    plan = AssistantPlan(assistant_message="ok", actions=[])
    metadata = ProviderResponseMetadata(
        provider="stub",
        model="stub",
        latency_seconds=0.0,
    )
    clean_iter = OrchestrationIteration(
        iteration=1,
        context=context,
        plan=plan,
        planning_metadata=metadata,
        execution_results=[],
    )
    assert (
        _status_for(
            LoopStopReason.MAX_ITERATIONS,
            executed=3,
            failed=2,  # earlier iterations failed
            iterations=[clean_iter],
        )
        is SessionStatus.COMPLETED_INCONCLUSIVE
    )


def test_session_status_max_iterations_with_failed_final_is_failed() -> None:
    from foundation.models import (
        AssistantPlan,
        ContextSnapshot,
        ExecutionResult,
        OrchestrationIteration,
        ProviderResponseMetadata,
    )

    context = ContextSnapshot(
        workspace_root="/tmp/w",
        request_cwd="/tmp/w",
        approval_mode="prompt",
    )
    plan = AssistantPlan(assistant_message="tried", actions=[])
    metadata = ProviderResponseMetadata(
        provider="stub",
        model="stub",
        latency_seconds=0.0,
    )
    failed_iter = OrchestrationIteration(
        iteration=4,
        context=context,
        plan=plan,
        planning_metadata=metadata,
        execution_results=[
            ExecutionResult(
                action_id="a1",
                status=ExecutionStatus.FAILED,
                summary="still failing",
                error="boom",
            )
        ],
    )
    assert (
        _status_for(
            LoopStopReason.MAX_ITERATIONS,
            failed=4,
            iterations=[failed_iter],
        )
        is SessionStatus.FAILED
    )


# ---------------------------------------------------------------------------
# Commit-approval runtime invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("please commit the fix", True),
        ("Fix and commit it", True),
        ("stage and commit the change", True),
        ("git commit", True),
        ("committed the fix", True),
        ("stop for approval", True),
        ("approve the change", True),
        ("show me the git status", False),
        ("stage the required files", False),
        ("just read the file", False),
    ],
)
def test_has_commit_intent_heuristic(message: str, expected: bool) -> None:
    assert RequestOrchestrator._has_commit_intent(message) is expected


def test_session_status_governance_notice_forces_pending_approval() -> None:
    from foundation.models import GovernanceNotice, GovernanceNoticeCode

    notice = GovernanceNotice(
        code=GovernanceNoticeCode.COMMIT_APPROVAL_MISSING,
        message="no commit performed",
        staged_paths=["src/pkg/__init__.py"],
    )
    assert (
        RequestOrchestrator._session_status_for_result(
            # A classification that would otherwise be COMPLETED:
            summary=_build_summary_stub(executed=1),
            stop_reason=LoopStopReason.ZERO_ACTION_PLAN,
            iterations=[],
            governance_notice=notice,
        )
        is SessionStatus.PENDING_APPROVAL
    )


def _build_summary_stub(
    *,
    executed: int = 0,
    pending: int = 0,
    failed: int = 0,
) -> Any:
    from foundation.models import OrchestrationSummary

    return OrchestrationSummary(
        executed_actions=executed,
        pending_approval_actions=pending,
        blocked_actions=0,
        failed_actions=failed,
        skipped_actions=0,
        total_iterations=1,
        total_actions_planned=executed + pending + failed,
        text="stub",
    )


def test_commit_approval_invariant_fires_when_staged_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: user asked to commit, model staged but forgot commit."""
    import subprocess

    from foundation.models import SessionStatus

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    git("add", ".")
    git("commit", "-q", "-m", "seed")

    target = workspace / "file.txt"
    target.write_text("edited\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Staging the edit.",
                    "actions": [
                        {
                            "id": "stage_it",
                            "kind": "tool_call",
                            "summary": "Stage the edit",
                            "tool_call": {
                                "capability_id": "foundation.git.stage",
                                "arguments": {"paths": ["file.txt"]},
                            },
                        }
                    ],
                }
            )
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    runtime = CountingShellRuntime(workspace_root=workspace)
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    orchestrator = RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=ApprovalMode.AUTO,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="please commit the edit"),
    )

    assert result.governance_notice is not None
    assert result.governance_notice.code.value == "commit_approval_missing"
    assert "file.txt" in result.governance_notice.staged_paths
    sessions = history_store.list_sessions(limit=5)
    assert sessions[0].status is SessionStatus.PENDING_APPROVAL


def test_commit_approval_invariant_silent_without_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No commit intent → no governance notice, even with staged files."""
    import subprocess

    from foundation.models import SessionStatus

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    git("add", ".")
    git("commit", "-q", "-m", "seed")

    (workspace / "file.txt").write_text("edited\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Staging.",
                    "actions": [
                        {
                            "id": "stage_it",
                            "kind": "tool_call",
                            "summary": "Stage the edit",
                            "tool_call": {
                                "capability_id": "foundation.git.stage",
                                "arguments": {"paths": ["file.txt"]},
                            },
                        }
                    ],
                }
            )
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    runtime = CountingShellRuntime(workspace_root=workspace)
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    orchestrator = RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=ApprovalMode.AUTO,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="just stage the edit for me"),
    )

    assert result.governance_notice is None
    sessions = history_store.list_sessions(limit=5)
    assert sessions[0].status is SessionStatus.COMPLETED


def test_commit_approval_invariant_silent_when_commit_planned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit intent present AND commit action planned → no notice."""
    import subprocess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    git("add", ".")
    git("commit", "-q", "-m", "seed")

    (workspace / "file.txt").write_text("edited\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Staging and committing.",
                    "actions": [
                        {
                            "id": "stage_it",
                            "kind": "tool_call",
                            "summary": "Stage the edit",
                            "tool_call": {
                                "capability_id": "foundation.git.stage",
                                "arguments": {"paths": ["file.txt"]},
                            },
                        },
                        {
                            "id": "commit_it",
                            "kind": "tool_call",
                            "summary": "Commit the edit",
                            "requires_approval": True,
                            "approval_reason": "commit requires approval",
                            "tool_call": {
                                "capability_id": "foundation.git.commit",
                                "arguments": {"message": "edit file"},
                            },
                        },
                    ],
                }
            )
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    runtime = CountingShellRuntime(workspace_root=workspace)
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    approval_service = ApprovalService(mode=ApprovalMode.AUTO_EXCEPT_COMMIT)
    orchestrator = RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=ApprovalMode.AUTO_EXCEPT_COMMIT,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        approval_service=approval_service,
        history_store=history_store,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="please commit the edit"),
    )

    # Commit was planned and is waiting on approval — no invariant intervention.
    assert result.governance_notice is None
    assert result.summary.pending_approval_actions == 1


def test_commit_intent_zero_action_plan_is_repaired_to_commit_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When staged changes remain, zero-action completion is rejected and repaired."""
    import subprocess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    git("add", ".")
    git("commit", "-q", "-m", "seed")

    (workspace / "file.txt").write_text("edited\n", encoding="utf-8")

    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Staging the edit.",
                    "actions": [
                        {
                            "id": "stage_it",
                            "kind": "tool_call",
                            "summary": "Stage the edit",
                            "tool_call": {
                                "capability_id": "foundation.git.stage",
                                "arguments": {"paths": ["file.txt"]},
                            },
                        }
                    ],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "The fix is staged and ready for commit approval.",
                    "actions": [],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "The fix is staged. I will pause for commit approval.",
                    "actions": [
                        {
                            "id": "commit_it",
                            "kind": "tool_call",
                            "summary": "Commit the staged edit",
                            "requires_approval": True,
                            "approval_reason": "commit requires explicit approval",
                            "tool_call": {
                                "capability_id": "foundation.git.commit",
                                "arguments": {"message": "edit file"},
                            },
                        }
                    ],
                }
            ),
        ]
    )
    history_store = HistoryStore(database_path=tmp_path / "history.sqlite3")
    runtime = CountingShellRuntime(workspace_root=workspace)
    tool_service = LocalToolService(
        workspace_root=workspace,
        default_timeout_seconds=5,
        capture_limit_kb=64,
    )
    orchestrator = RequestOrchestrator(
        workspace_root=workspace,
        approval_mode=ApprovalMode.AUTO_EXCEPT_COMMIT,
        provider=provider,
        shell_runtime=runtime,
        tool_service=tool_service,
        approval_service=ApprovalService(mode=ApprovalMode.AUTO_EXCEPT_COMMIT),
        history_store=history_store,
    )

    result = orchestrator.orchestrate(
        UserRequest(message="fix the file and stop for commit approval"),
    )

    assert len(provider.calls) == 3
    assert result.stop_reason is LoopStopReason.PENDING_APPROVAL
    assert result.governance_notice is None
    assert result.summary.pending_approval_actions == 1
    assert any(
        action.tool_call is not None
        and action.tool_call.capability_id == "foundation.git.commit"
        and action.requires_approval
        for action in result.iterations[-1].plan.actions
    )


def test_unwrap_generated_file_body_extracts_plan_wrapped_content() -> None:
    from foundation.services.orchestrator import _unwrap_generated_file_body

    wrapped = json.dumps(
        {
            "assistant_message": "writing the file",
            "actions": [
                {
                    "id": "w",
                    "kind": "tool_call",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {"path": "r.md", "content": "# Real Title\n\nbody text"},
                    },
                }
            ],
        }
    )
    assert _unwrap_generated_file_body(wrapped) == "# Real Title\n\nbody text"


def test_unwrap_generated_file_body_passes_through_plain_and_real_json() -> None:
    from foundation.services.orchestrator import _unwrap_generated_file_body

    assert _unwrap_generated_file_body("# Just markdown\n") == "# Just markdown\n"
    # A legitimate JSON file (no actions array) must be left untouched.
    config = '{"key": "value", "nested": {"a": 1}}'
    assert _unwrap_generated_file_body(config) == config


def test_orchestrator_unwraps_plan_wrapped_generated_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = "# Clean Report\n\nThe actual body of the report.\n"
    wrapped_body = json.dumps(
        {
            "assistant_message": "writing",
            "actions": [
                {
                    "id": "w",
                    "kind": "tool_call",
                    "tool_call": {
                        "capability_id": "foundation.file.write",
                        "arguments": {"path": "report.md", "content": clean},
                    },
                }
            ],
        }
    )
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Writing the report.",
                    "actions": [
                        {
                            "id": "write_report",
                            "kind": "tool_call",
                            "summary": "Write the report",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {
                                    "path": "report.md",
                                    "content_brief": "a report about the project",
                                },
                            },
                        }
                    ],
                }
            ),
            _text_response(wrapped_body),
        ]
    )
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)

    orchestrator.orchestrate(UserRequest(message="write a report"))

    # The plan-wrapped generation is unwrapped to the clean file body.
    assert (workspace_root / "report.md").read_text(encoding="utf-8") == clean


def test_orchestrator_not_found_surfaces_siblings_for_self_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Reading the report.",
                    "actions": [_read_action("read_wrong", "res/wrong-name.md")],
                }
            ),
            _provider_response(
                {
                    "assistant_message": "Retrying with the real filename.",
                    "actions": [_read_action("read_right", "res/right-name.md")],
                }
            ),
        ]
    )
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)
    (workspace_root / "res").mkdir()
    (workspace_root / "res" / "right-name.md").write_text("# Found me\n", encoding="utf-8")

    result = orchestrator.orchestrate(UserRequest(message="read the report in res"))

    # The not-found error from iteration 1 carried the real filename into
    # iteration 2's planning context, enabling self-correction.
    second_plan_text = "\n".join(m.content for m in provider.calls[1].messages)
    assert "right-name.md" in second_plan_text
    # The corrected read then succeeded.
    assert any(
        r.status is ExecutionStatus.EXECUTED and r.artifact_type is ExecutionArtifactType.FILE_READ
        for r in result.execution_results
    )


def test_tool_result_preview_surfaces_reads_not_writes() -> None:
    from foundation.services.orchestrator import _tool_result_preview

    # File-read content is surfaced verbatim.
    assert (
        _tool_result_preview(
            {"path": "a.md", "content": "# Hi\nbody"}, ExecutionArtifactType.FILE_READ
        )
        == "# Hi\nbody"
    )
    # Structured read-only results (search) are surfaced as compact JSON.
    search_preview = _tool_result_preview(
        {"matches": ["a.py:1: needle"]}, ExecutionArtifactType.SEARCH
    )
    assert "a.py:1: needle" in search_preview
    # Writes are NOT echoed back (would only re-bloat the prompt).
    assert (
        _tool_result_preview(
            {"path": "a.md", "content": "huge body"}, ExecutionArtifactType.FILE_WRITE
        )
        == ""
    )
    assert _tool_result_preview(None, ExecutionArtifactType.FILE_READ) == ""


def test_orchestrator_surfaces_file_read_content_to_next_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Reading the note.",
                    "actions": [_read_action("read_note", "note.txt")],
                }
            )
            # iteration 2: StubProvider returns a zero-action completion by default.
        ]
    )
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)
    (workspace_root / "note.txt").write_text("SECRET-CONTENT-12345\n", encoding="utf-8")

    result = orchestrator.orchestrate(UserRequest(message="read note.txt"))

    # The read succeeded AND its content reached the planner's next iteration —
    # previously the observation was blank and the model re-read forever.
    assert any(
        r.status is ExecutionStatus.EXECUTED and r.artifact_type is ExecutionArtifactType.FILE_READ
        for r in result.execution_results
    )
    second_plan_text = "\n".join(m.content for m in provider.calls[1].messages)
    assert "SECRET-CONTENT-12345" in second_plan_text


def test_repeated_successful_file_read_stops_as_no_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_response_1 = _provider_response(
        {
            "assistant_message": "Reading the note.",
            "actions": [_read_action("read_note_1", "note.txt")],
        }
    )
    read_response_2 = _provider_response(
        {
            "assistant_message": "Reading the note again.",
            "actions": [_read_action("read_note_2", "note.txt")],
        }
    )
    read_response_3 = _provider_response(
        {
            "assistant_message": "Reading the note yet again.",
            "actions": [_read_action("read_note_3", "note.txt")],
        }
    )
    provider = StubProvider([read_response_1, read_response_2, read_response_3])
    orchestrator, _, workspace_root = _orchestrator(tmp_path, monkeypatch, provider)
    (workspace_root / "note.txt").write_text("SECRET-CONTENT-12345\n", encoding="utf-8")

    result = orchestrator.orchestrate(UserRequest(message="read note.txt"))

    assert result.stop_reason is LoopStopReason.NO_PROGRESS
    assert len(result.iterations) == 2
    plan_calls = [
        call
        for call in provider.calls
        if call.response_format is ProviderResponseFormat.JSON_OBJECT
    ]
    assert len(plan_calls) == 2
