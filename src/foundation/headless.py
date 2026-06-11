"""Headless worker mode: contract task.json in, NDJSON events + result.json out.

Implements the worker side of the agent-task-contract v0.1 spec: the existing
plan→execute→observe→replan loop is driven from a ``CodingWorkerTask`` envelope,
every observer event is stamped onto the contract event stream at
``<workspace>/.events/<task_id>.ndjson``, and a ``CodingWorkerResult`` is written
on exit. Headless mode never prompts a terminal: approval-requiring actions stay
pending and the run terminates with status ``pending_approval``; the worker never
commits and never pushes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_task_contract import (
    CONTRACT_VERSION,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    CommandRecord,
    EventType,
    TaskState,
    Usage,
    Verification,
    check_supported,
)
from agent_task_contract import (
    VerificationOutcome as ContractVerificationOutcome,
)
from pydantic import ValidationError

from foundation import __version__ as WORKER_VERSION
from foundation.models import (
    LoopStopReason,
    OrchestrationResult,
    ProviderPrompt,
    ProviderResponse,
    UserRequest,
)
from foundation.observability import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_ITERATION_COMPLETED,
    EVENT_PLAN_FINISHED,
    EVENT_PLAN_STARTED,
    EVENT_SHELL_EXECUTION_FAILED,
    EVENT_SHELL_EXECUTION_FINISHED,
    EVENT_SHELL_EXECUTION_STARTED,
    EVENT_TOOL_EXECUTION_STARTED,
)
from foundation.services import LocalToolService, ShellRuntime
from foundation.services.capabilities import CapabilityRegistry, CapabilityStore
from foundation.services.history import TraceStore
from foundation.services.orchestrator import RequestOrchestrator
from foundation.services.provider import ProviderAdapter, build_provider_adapter
from foundation.settings import ApprovalMode, AppSettings

SUPPORTED_CONTRACT_RANGE = ">=0.1,<0.2"
DEFAULT_HEARTBEAT_SECONDS = 10.0

# Exit codes mirror the terminal state (plan Stage 4 relies on this mapping).
EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_PENDING_APPROVAL = 4
EXIT_REJECTED = 5

_EXIT_BY_STATUS: dict[TaskState, int] = {
    TaskState.COMPLETED: EXIT_COMPLETED,
    TaskState.FAILED: EXIT_FAILED,
    TaskState.PENDING_APPROVAL: EXIT_PENDING_APPROVAL,
    TaskState.REJECTED: EXIT_REJECTED,
}


def _utc_now_rfc3339() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ContractEventStream:
    """Thread-safe, contract-stamped NDJSON event writer (spec §4)."""

    def __init__(self, path: Path, *, task_id: str, trace_id: str) -> None:
        self.path = path
        self._task_id = task_id
        self._trace_id = trace_id
        self._seq = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any stale stream from a previous run of the same task id.
        path.write_text("")

    def emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            envelope = {
                "contract_version": CONTRACT_VERSION,
                "event_id": str(uuid.uuid4()),
                "seq": self._seq,
                "task_id": self._task_id,
                "trace_id": self._trace_id,
                "ts": _utc_now_rfc3339(),
                "type": event_type.value,
                "payload": payload,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


class ContractEventSink:
    """Map fcli observer events onto the contract event stream.

    Receives payloads already passed through the observability redaction
    pipeline (ObserverService redacts before dispatching to sinks), so
    redaction is preserved as-is.
    """

    def __init__(self, stream: ContractEventStream) -> None:
        self._stream = stream
        self.phase = "starting"
        self._command_purposes: dict[str, str] = {}
        self.commands: list[CommandRecord] = []
        self._lock = threading.Lock()

    def __call__(self, event_name: str, payload: Mapping[str, Any]) -> None:
        if event_name == EVENT_PLAN_STARTED:
            self.phase = "planning"
        elif event_name in (EVENT_SHELL_EXECUTION_STARTED, EVENT_TOOL_EXECUTION_STARTED):
            self.phase = "executing"
        elif event_name == EVENT_ITERATION_COMPLETED:
            self.phase = "observing"

        if event_name == EVENT_PLAN_FINISHED:
            iteration = payload.get("iteration")
            action_count = payload.get("action_count")
            self._stream.emit(
                EventType.PLAN_CREATED,
                {"steps": [f"iteration {iteration}: {action_count} action(s) planned"]},
            )
        elif event_name == EVENT_SHELL_EXECUTION_STARTED:
            command = str(payload.get("command_preview", ""))
            action_id = str(payload.get("action_id", ""))
            purpose = f"shell action {action_id}"
            with self._lock:
                self._command_purposes[action_id] = purpose
            self._stream.emit(
                EventType.COMMAND_START,
                {"command": command, "purpose": purpose},
            )
        elif event_name in (EVENT_SHELL_EXECUTION_FINISHED, EVENT_SHELL_EXECUTION_FAILED):
            action_id = str(payload.get("action_id", ""))
            command = str(payload.get("command_preview", ""))
            exit_code = int(payload.get("exit_code") or 0)
            duration_seconds = float(payload.get("duration_seconds") or 0.0)
            record = CommandRecord(
                command=command,
                exit_code=exit_code,
                purpose=self._command_purposes.get(action_id, f"shell action {action_id}"),
            )
            with self._lock:
                self.commands.append(record)
            self._stream.emit(
                EventType.COMMAND_RESULT,
                {
                    "command": command,
                    "exit_code": exit_code,
                    "duration_ms": int(duration_seconds * 1000),
                    "stdout_tail": str(payload.get("stdout_preview") or ""),
                    "stderr_tail": str(payload.get("stderr_preview") or ""),
                },
            )
        elif event_name == EVENT_APPROVAL_REQUESTED:
            action_id = str(payload.get("action_id", ""))
            risk = ", ".join(str(r) for r in payload.get("risk_categories", []) or [])
            self._stream.emit(
                EventType.APPROVAL_REQUESTED,
                {
                    "action": action_id,
                    "reason": f"approval required (risk: {risk or 'unspecified'}); "
                    "headless mode never prompts — stopping as pending_approval",
                },
            )


class _Heartbeat:
    """Emit a contract heartbeat with the current phase every N seconds (Q3)."""

    def __init__(
        self,
        stream: ContractEventStream,
        sink: ContractEventSink,
        interval_seconds: float,
    ) -> None:
        self._stream = stream
        self._sink = sink
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="contract-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._stream.emit(EventType.HEARTBEAT, {"phase": self._sink.phase})

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_git_repo(workspace: Path) -> bool:
    return _git(workspace, "rev-parse", "--is-inside-work-tree").returncode == 0


def _collect_patch(workspace: Path, task_id: str) -> tuple[Artifact | None, list[str]]:
    """Write the patch artifact (Q5) and return it plus the changed-file list.

    ``git add -N`` records intent-to-add for untracked files so the diff covers
    them; nothing is ever committed or pushed in headless mode.
    """
    if not _is_git_repo(workspace):
        return None, []
    # Contract bookkeeping (.events/, .artifacts/) must never appear in the patch.
    excludes = (":(exclude).events", ":(exclude).artifacts")
    _git(workspace, "add", "-N", "--", ".", *excludes)
    changed_proc = _git(workspace, "diff", "--name-only", "--", ".", *excludes)
    changed_files = [line for line in changed_proc.stdout.splitlines() if line.strip()]
    # --binary keeps the patch appliable (git apply --index) even when untracked
    # binary files were intent-added; applying the patch is the approval action (Q5).
    diff_proc = _git(workspace, "diff", "--binary", "--", ".", *excludes)
    if not diff_proc.stdout.strip():
        return None, changed_files
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifacts_dir / f"{task_id}.patch"
    patch_path.write_text(diff_proc.stdout, encoding="utf-8")
    artifact = Artifact(
        kind="patch",
        path=str(patch_path.relative_to(workspace)),
        sha256=_sha256_file(patch_path),
    )
    return artifact, changed_files


def _registry_manifest_fingerprint(registry: CapabilityRegistry) -> str:
    """SHA-256 over the full capability-manifest set (G7 surfacing)."""
    digest = hashlib.sha256()
    manifests = sorted(
        registry.list_capabilities(),
        key=lambda manifest: (str(manifest.capability_id), str(manifest.version)),
    )
    for manifest in manifests:
        payload = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
        digest.update(payload)
    return digest.hexdigest()


class _MeteredProvider:
    """Wrap a provider to meter per-task usage (G8): call count + token totals.

    The contract reserves ``Usage`` in the result envelope; this fills it from
    every ``complete`` call (planning *and* preflight review) so the supervisor
    can answer cost per task. ``cost_usd`` stays None — turning tokens into
    dollars needs a per-model price map, which is supervisor-side and later.
    """

    def __init__(self, inner: ProviderAdapter) -> None:
        self._inner = inner
        self._calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._saw_tokens = False

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        response = self._inner.complete(prompt)
        self._calls += 1
        usage = response.metadata.usage
        if usage is not None:
            if usage.input_tokens is not None:
                self._input_tokens += usage.input_tokens
                self._saw_tokens = True
            if usage.output_tokens is not None:
                self._output_tokens += usage.output_tokens
                self._saw_tokens = True
        return response

    def snapshot(self) -> Usage:
        return Usage(
            provider_calls=self._calls,
            input_tokens=self._input_tokens if self._saw_tokens else None,
            output_tokens=self._output_tokens if self._saw_tokens else None,
            cost_usd=None,
        )


def _usage_snapshot(metered: _MeteredProvider | None) -> Usage | None:
    return metered.snapshot() if metered is not None else None


class _Finalizer:
    """Single-shot guard so the deadline thread and the main path never both finalize."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False

    def claim(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True


def _status_for(result: OrchestrationResult) -> TaskState:
    if result.stop_reason is LoopStopReason.PENDING_APPROVAL:
        return TaskState.PENDING_APPROVAL
    notice = result.verification_notice
    verified = notice is not None and notice.outcome.value == "passed"
    if verified and result.stop_reason is LoopStopReason.ZERO_ACTION_PLAN:
        return TaskState.COMPLETED
    return TaskState.FAILED


def _verification_for(result: OrchestrationResult) -> Verification:
    notice = result.verification_notice
    if notice is None:
        return Verification(
            outcome=ContractVerificationOutcome.NOT_ATTEMPTED,
            details="no verification notice produced",
        )
    commands = ", ".join(notice.verification_commands_run) or "none"
    details = notice.reason or f"verification commands run: {commands}"
    # fcli's taxonomy maps 1:1 onto the contract's (Keep List #5).
    return Verification(
        outcome=ContractVerificationOutcome(notice.outcome.value),
        details=details,
    )


def _write_result(out_path: Path, result: CodingWorkerResult) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _finalize(
    *,
    task: CodingWorkerTask,
    out_path: Path,
    stream: ContractEventStream,
    sink: ContractEventSink,
    status: TaskState,
    summary: str,
    verification: Verification,
    changed_files: list[str],
    extra_artifacts: list[Artifact],
    terminal_reason: str | None = None,
    usage: Usage | None = None,
) -> int:
    workspace = Path(task.workspace)
    terminal_payload: dict[str, Any] = {"status": status.value, "summary": summary}
    if terminal_reason is not None:
        terminal_payload["reason"] = terminal_reason
    stream.emit(EventType.TASK_TERMINAL, terminal_payload)
    event_log = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace))
        if stream.path.is_relative_to(workspace)
        else str(stream.path),
        sha256=_sha256_file(stream.path),
    )
    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=list(sink.commands),
        verification=verification,
        artifacts=[event_log, *extra_artifacts],
        usage=usage,
    )
    _write_result(out_path, result)
    return _EXIT_BY_STATUS.get(status, EXIT_FAILED)


def run_headless_task(
    task_path: Path,
    out_path: Path,
    *,
    settings: AppSettings,
    provider: ProviderAdapter | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> int:
    """Run one contract task end-to-end. Returns the process exit code."""
    try:
        raw_text = task_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"headless: cannot read task file {task_path}: {exc}. "
            "Remediation: pass --task-file pointing at a contract task.json.",
            flush=True,
        )
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print(
            "headless: task envelope is missing task_id/trace_id/workspace; "
            "cannot open an event stream. Remediation: fix the dispatching supervisor.",
            flush=True,
        )
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(
            f"headless: workspace {workspace} does not exist or is not a directory. "
            "Remediation: the supervisor must create the worktree before dispatch.",
            flush=True,
        )
        return EXIT_INVOCATION_ERROR

    stream = ContractEventStream(
        workspace / ".events" / f"{task_id}.ndjson",
        task_id=task_id,
        trace_id=trace_id,
    )
    sink = ContractEventSink(stream)

    def _reject(reason: str) -> int:
        stream.emit(
            EventType.TASK_REJECTED,
            {
                "reason": reason,
                "worker_version": WORKER_VERSION,
                "supported_range": SUPPORTED_CONTRACT_RANGE,
            },
        )
        stream.emit(
            EventType.TASK_TERMINAL,
            {"status": TaskState.REJECTED.value, "summary": reason, "reason": "rejected"},
        )
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task_id,
            trace_id=trace_id,
            status=TaskState.REJECTED,
            summary=reason,
            changed_files=[],
            commands=[],
            verification=Verification(
                outcome=ContractVerificationOutcome.NOT_ATTEMPTED,
                details="task rejected before execution",
            ),
            artifacts=[
                Artifact(
                    kind="event_log",
                    path=str(stream.path.relative_to(workspace)),
                    sha256=_sha256_file(stream.path),
                )
            ],
        )
        _write_result(out_path, result)
        return EXIT_REJECTED

    task_version = str(raw.get("contract_version") or "")
    try:
        skew = check_supported(task_version, SUPPORTED_CONTRACT_RANGE)
    except ValueError:
        return _reject(
            f"task contract_version {task_version!r} is not a valid semver string; "
            f"worker supports {SUPPORTED_CONTRACT_RANGE}."
        )
    if skew is not None:
        return _reject(str(skew))

    try:
        task = CodingWorkerTask.model_validate(raw)
    except ValidationError as exc:
        return _reject(f"task envelope failed validation: {exc}")

    workspace = Path(task.workspace).expanduser().resolve()
    metered_provider: _MeteredProvider | None = None
    try:
        tool_service = LocalToolService(
            workspace_root=workspace,
            default_timeout_seconds=min(settings.shell.default_timeout_seconds, 30),
            capture_limit_kb=settings.shell.capture_limit_kb,
            pass_through_foundation_env=settings.shell.pass_through_foundation_env,
        )
        shell_runtime = ShellRuntime(
            workspace_root=workspace,
            default_timeout_seconds=settings.shell.default_timeout_seconds,
            max_timeout_seconds=settings.shell.max_timeout_seconds,
            allow_pty=False,
            capture_limit_kb=settings.shell.capture_limit_kb,
            enforce_workspace_boundary=True,
        )
        capability_registry = CapabilityRegistry(
            store=CapabilityStore(settings.app.data_dir / "capabilities"),
            tool_service=tool_service,
        )
        history_store = TraceStore(
            database_path=settings.history.database_path,
            retention_days=settings.history.retention_days,
            max_entries=settings.history.max_entries,
        )
        resolved_provider = provider if provider is not None else build_provider_adapter(settings)
        metered_provider = _MeteredProvider(resolved_provider)  # G8: per-task usage

        # Q8 true resume: a supervisor-granted approval verdict lets this run
        # proceed past the policy gate that previously stopped it. We use
        # AUTO_EXCEPT_COMMIT (not blanket AUTO): commit, network, and
        # outside-workspace actions still require explicit approval and will
        # re-stop pending_approval — the resume is bounded, not a blank cheque.
        # No verdict (a fresh task) keeps the v1 MANUAL behaviour.
        resume_approved = task.approval_verdict is not None and task.approval_verdict.approved
        approval_mode = (
            ApprovalMode.AUTO_EXCEPT_COMMIT if resume_approved else ApprovalMode.MANUAL
        )

        orchestrator = RequestOrchestrator(
            workspace_root=workspace,
            approval_mode=approval_mode,
            provider=metered_provider,
            shell_runtime=shell_runtime,
            tool_service=tool_service,
            history_store=history_store,
            capability_registry=capability_registry,
            event_sink=sink,
            question_callback=None,
            max_loop_iterations=task.budget.max_iterations,
            max_total_actions=task.budget.max_actions,
        )
    except Exception as exc:  # noqa: BLE001 — setup failures must still leave evidence
        return _finalize(
            task=task,
            out_path=out_path,
            stream=stream,
            sink=sink,
            status=TaskState.FAILED,
            summary=f"worker setup failed: {exc}",
            verification=Verification(
                outcome=ContractVerificationOutcome.NOT_ATTEMPTED,
                details="failed before execution (provider/services construction)",
            ),
            changed_files=[],
            extra_artifacts=[],
            terminal_reason="worker_setup_failed",
            usage=_usage_snapshot(metered_provider),
        )

    stream.emit(
        EventType.TASK_START,
        {
            "worker_version": WORKER_VERSION,
            "manifest_fingerprint": _registry_manifest_fingerprint(capability_registry),
        },
    )

    finalizer = _Finalizer()
    heartbeat = _Heartbeat(stream, sink, heartbeat_seconds)
    heartbeat.start()

    def _self_deadline() -> None:
        # Defense-in-depth (Q3/Q4): the supervisor backstop also enforces this.
        if not finalizer.claim():
            return
        heartbeat.stop()
        exit_code = _finalize(
            task=task,
            out_path=out_path,
            stream=stream,
            sink=sink,
            status=TaskState.FAILED,
            summary=(
                f"budget wall_clock_seconds={task.budget.wall_clock_seconds} exceeded; "
                "worker self-terminated"
            ),
            verification=Verification(
                outcome=ContractVerificationOutcome.NOT_ATTEMPTED,
                details="wall-clock budget exceeded before verification",
            ),
            changed_files=[],
            extra_artifacts=[],
            terminal_reason="wall_clock_exceeded",
            usage=_usage_snapshot(metered_provider),
        )
        os._exit(exit_code)

    deadline = threading.Timer(float(task.budget.wall_clock_seconds), _self_deadline)
    deadline.daemon = True
    deadline.start()

    try:
        request = UserRequest(message=task.instructions, cwd=workspace)
        result = orchestrator.orchestrate(request)
    except Exception as exc:  # noqa: BLE001 — any crash must still produce evidence
        deadline.cancel()
        if not finalizer.claim():
            return EXIT_FAILED
        heartbeat.stop()
        return _finalize(
            task=task,
            out_path=out_path,
            stream=stream,
            sink=sink,
            status=TaskState.FAILED,
            summary=f"worker error: {exc}",
            verification=Verification(
                outcome=ContractVerificationOutcome.NOT_ATTEMPTED,
                details="run aborted before verification",
            ),
            changed_files=[],
            extra_artifacts=[],
            terminal_reason="exception",
            usage=_usage_snapshot(metered_provider),
        )

    deadline.cancel()
    if not finalizer.claim():
        return EXIT_FAILED
    heartbeat.stop()
    sink.phase = "finishing"

    verification = _verification_for(result)
    notice = result.verification_notice
    # Surface the exact verification command(s) so the supervisor can re-run them
    # against the patch (contract G10). Additive payload key — the contract
    # Event keeps unknown payload keys, so this is not a contract change.
    verification_commands = list(notice.verification_commands_run) if notice is not None else []
    stream.emit(
        EventType.VERIFY_RESULT,
        {
            "outcome": verification.outcome.value,
            "details": verification.details,
            "commands": verification_commands,
        },
    )

    patch_artifact, changed_files = _collect_patch(workspace, task.task_id)
    status = _status_for(result)
    extra_artifacts = [patch_artifact] if patch_artifact is not None else []
    return _finalize(
        task=task,
        out_path=out_path,
        stream=stream,
        sink=sink,
        status=status,
        summary=result.assistant_message.content,
        verification=verification,
        changed_files=changed_files,
        extra_artifacts=extra_artifacts,
        usage=_usage_snapshot(metered_provider),
    )
