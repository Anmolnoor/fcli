"""Hermetic tests for headless worker mode (contract v0.1).

No network, no live LLM: the provider is a scripted stub, verification commands
are fake binaries on PATH, and the workspace is a throwaway git repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from agent_task_contract import (
    CONTRACT_VERSION,
    CodingWorkerResult,
    Event,
    TaskState,
)

from foundation.headless import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_PENDING_APPROVAL,
    EXIT_REJECTED,
    run_headless_task,
)
from foundation.models import ProviderPrompt, ProviderResponse, ProviderResponseMetadata
from foundation.settings import AppSection, AppSettings, MonitorSection


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


class StubProvider:
    """Scripted provider; auto-accepts plan-review preflights (mirrors test_orchestrator)."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[ProviderPrompt] = []

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        if prompt.schema_name == "assistant_plan_review" and not (
            self._responses
            and isinstance(self._responses[0].structured_output, dict)
            and "decision" in self._responses[0].structured_output
        ):
            return _provider_response(
                {"decision": "accept", "reason": "Stub preflight accepted the plan."}
            )
        self.calls.append(prompt)
        if not self._responses:
            return _provider_response({"assistant_message": "Done.", "actions": []})
        return self._responses.pop(0)


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        app=AppSection(
            workspace_root=tmp_path / "workspace",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "logs",
        ),
        monitor=MonitorSection(enabled=False, events_dir=tmp_path / "monitor-events"),
    )


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "existing.py").write_text("value = 0\n")
    _git(workspace, "add", "existing.py")
    _git(workspace, "commit", "-qm", "seed")
    return workspace


def _task_envelope(workspace: Path, **overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "task_id": "01976e10-0000-7000-8000-0000000000aa",
        "trace_id": "01976e10-0000-7000-8000-0000000000bb",
        "worker_kind": "coding",
        "workspace": str(workspace),
        "instructions": "Fix the failing test and make the suite pass.",
        "permissions": {"read": ["workspace"], "write": ["workspace"], "env_allowlist": []},
        "budget": {
            "wall_clock_seconds": 120,
            "max_iterations": 5,
            "max_actions": 20,
            "max_provider_calls": 20,
        },
    }
    envelope.update(overrides)
    return envelope


def _install_fake_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "pytest"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""\
        import sys
        print("4 passed")
        sys.exit(0)
        """)
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _run(
    tmp_path: Path,
    provider: StubProvider,
    envelope: dict[str, Any],
) -> tuple[int, Path, Path]:
    task_path = tmp_path / "task.json"
    out_path = tmp_path / "result.json"
    task_path.write_text(json.dumps(envelope))
    code = run_headless_task(
        task_path,
        out_path,
        settings=_settings(tmp_path),
        provider=provider,  # type: ignore[arg-type]
        heartbeat_seconds=600.0,
    )
    return code, out_path, Path(envelope["workspace"])


def _happy_provider() -> StubProvider:
    return StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Fixing the bug and verifying.",
                    "actions": [
                        {
                            "id": "a1",
                            "kind": "tool_call",
                            "summary": "Apply the fix",
                            "tool_call": {
                                "capability_id": "foundation.file.write",
                                "arguments": {
                                    "path": "existing.py",
                                    "content": "value = 1\n",
                                    "overwrite": True,
                                },
                            },
                        },
                        {
                            "id": "a2",
                            "kind": "shell",
                            "summary": "Run the test suite",
                            "shell": {"command": "pytest"},
                        },
                    ],
                }
            ),
            _provider_response(
                {"assistant_message": "Fixed and verified; suite passes.", "actions": []}
            ),
        ]
    )


def _read_events(workspace: Path, task_id: str) -> list[Event]:
    lines = (workspace / ".events" / f"{task_id}.ndjson").read_text().splitlines()
    return [Event.model_validate(json.loads(line)) for line in lines]


def test_happy_path_produces_contract_result_and_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch)
    head_before = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    code, out_path, _ = _run(tmp_path, _happy_provider(), _task_envelope(workspace))

    assert code == EXIT_COMPLETED
    result = CodingWorkerResult.model_validate_json(out_path.read_text())
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome.value == "passed"
    assert result.changed_files == ["existing.py"]
    kinds = {artifact.kind for artifact in result.artifacts}
    assert kinds == {"event_log", "patch"}
    assert result.commands and result.commands[0].command == "pytest"
    assert result.commands[0].exit_code == 0

    patch = next(a for a in result.artifacts if a.kind == "patch")
    patch_path = workspace / patch.path
    assert patch_path.is_file()
    assert "value = 1" in patch_path.read_text()

    # Repo HEAD untouched: no commit, no push, ever (Q5 / Keep List #9).
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == head_before

    events = _read_events(workspace, "01976e10-0000-7000-8000-0000000000aa")
    assert events[0].type.value == "task.start"
    assert events[0].payload["worker_version"]
    assert events[0].payload["manifest_fingerprint"]
    assert events[-1].type.value == "task.terminal"
    assert events[-1].payload["status"] == "completed"
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    types = [event.type.value for event in events]
    assert "verify.result" in types
    assert "command.result" in types

    # G10: the verify.result event surfaces the exact verification command(s) so a
    # supervisor can re-run them against the patch (additive payload key).
    verify_event = next(e for e in events if e.type.value == "verify.result")
    assert verify_event.payload["commands"] == ["pytest"]

    # Event-log artifact hash covers the file as written.
    event_log = next(a for a in result.artifacts if a.kind == "event_log")
    import hashlib

    digest = hashlib.sha256((workspace / event_log.path).read_bytes()).hexdigest()
    assert digest == event_log.sha256


def test_headless_never_reads_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingStdin:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"headless mode read stdin (.{name})")

    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdin", ExplodingStdin())

    code, out_path, _ = _run(tmp_path, _happy_provider(), _task_envelope(workspace))

    assert code == EXIT_COMPLETED
    assert CodingWorkerResult.model_validate_json(out_path.read_text()).status is (
        TaskState.COMPLETED
    )


def test_approval_required_action_ends_pending_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    head_before = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    provider = StubProvider(
        [
            _provider_response(
                {
                    "assistant_message": "Committing the change.",
                    "actions": [
                        {
                            "id": "a1",
                            "kind": "tool_call",
                            "summary": "Commit the fix",
                            "requires_approval": True,
                            "approval_reason": "git commit always requires approval",
                            "tool_call": {
                                "capability_id": "foundation.git.commit",
                                "arguments": {"message": "fix"},
                            },
                        }
                    ],
                }
            ),
        ]
    )

    code, out_path, _ = _run(tmp_path, provider, _task_envelope(workspace))

    assert code == EXIT_PENDING_APPROVAL
    result = CodingWorkerResult.model_validate_json(out_path.read_text())
    assert result.status is TaskState.PENDING_APPROVAL
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == head_before

    events = _read_events(workspace, "01976e10-0000-7000-8000-0000000000aa")
    types = [event.type.value for event in events]
    assert "approval.requested" in types
    assert events[-1].type.value == "task.terminal"
    assert events[-1].payload["status"] == "pending_approval"


def test_contract_version_skew_rejects_task(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    provider = StubProvider([])
    envelope = _task_envelope(workspace, contract_version="9.9.9")

    code, out_path, _ = _run(tmp_path, provider, envelope)

    assert code == EXIT_REJECTED
    result = CodingWorkerResult.model_validate_json(out_path.read_text())
    assert result.status is TaskState.REJECTED
    assert "9.9.9" in result.summary
    assert provider.calls == []

    events = _read_events(workspace, "01976e10-0000-7000-8000-0000000000aa")
    types = [event.type.value for event in events]
    assert types[0] == "task.rejected"
    assert events[-1].payload["status"] == "rejected"


def test_unknown_worker_kind_rejects_task(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    envelope = _task_envelope(workspace, worker_kind="curator")

    code, out_path, _ = _run(tmp_path, StubProvider([]), envelope)

    assert code == EXIT_REJECTED
    result = CodingWorkerResult.model_validate_json(out_path.read_text())
    assert result.status is TaskState.REJECTED
    assert "worker_kind" in result.summary


def test_budget_max_iterations_bounds_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    _install_fake_pytest(tmp_path, monkeypatch)
    # Provider always plans another action; budget must stop it after 1 iteration.
    looping_plan = {
        "assistant_message": "Still working.",
        "actions": [
            {
                "id": "a1",
                "kind": "shell",
                "summary": "Run the suite again",
                "shell": {"command": "pytest"},
            }
        ],
    }
    provider = StubProvider([_provider_response(looping_plan) for _ in range(4)])
    envelope = _task_envelope(workspace)
    envelope["budget"]["max_iterations"] = 1

    code, out_path, _ = _run(tmp_path, provider, envelope)

    assert code == EXIT_FAILED
    result = CodingWorkerResult.model_validate_json(out_path.read_text())
    assert result.status is TaskState.FAILED
    events = _read_events(workspace, "01976e10-0000-7000-8000-0000000000aa")
    plan_events = [event for event in events if event.type.value == "plan.created"]
    assert len(plan_events) == 1


def test_missing_task_file_is_invocation_error(tmp_path: Path) -> None:
    code = run_headless_task(
        tmp_path / "absent.json",
        tmp_path / "result.json",
        settings=_settings(tmp_path),
        provider=StubProvider([]),  # type: ignore[arg-type]
    )
    assert code == 2
