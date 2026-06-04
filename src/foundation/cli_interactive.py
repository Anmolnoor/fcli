"""Interactive chat session loop for Foundation CLI."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from foundation.cli_rendering import (
    _emit_output_event,
    _preview_transcript_text,
    _render_availability,
    _render_chat_turn,
    _render_execution_summary,
    _render_history_list,
    _render_interactive_detail,
    console,
)
from foundation.cli_runtime import (
    _build_capability_registry,
    _build_history_store,
    _build_session_manager,
    _build_shell_runtime,
    _build_tool_service,
    _execute_chat_request,
    _handle_gap_handoff,
    _prompt_for_approval,
    _resolve_cli_request_cwd,
)
from foundation.models import (
    ActionKind,
    ApprovalDecisionStatus,
    BrainSession,
    InteractiveDetailCommand,
    MemoryEnvelope,
    MemoryLayer,
    MemorySource,
    OrchestrationResult,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    ProviderMessage,
    RenderMode,
    ResumeTarget,
    SessionKind,
    SessionSnapshot,
    SessionStatus,
    ShellAction,
    ShellActionMode,
)
from foundation.services import (
    ApprovalService,
    ExecutionMode,
    GuardrailPolicyEngine,
    HistoryStore,
    OrchestrationError,
    OrchestrationPlanError,
    ProviderError,
    SessionManager,
    ShellCommandRequest,
    ShellCommandResult,
    ShellExecutionCancelled,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ShellRuntime,
)
from foundation.settings import ApprovalMode, AppSettings, render_settings_payload

_REPL_DEFAULT_HISTORY_LIMIT = 10


_REPL_HISTORY_FILENAME = "repl-history.txt"


_REPL_SHELL_PREFIX = "!"


_REPL_COMMAND_COMPLETIONS: dict[str, Any] = {
    "/actions": None,
    "/approval": {
        "auto": None,
        "manual": None,
        "prompt": None,
    },
    "/clear": None,
    "/compact": None,
    "/config": {
        "locations": None,
    },
    "/cwd": None,
    "/exit": None,
    "/help": None,
    "/history": None,
    "/memory": {
        "append": {
            "global": None,
            "project": None,
        },
        "set": {
            "global": None,
            "project": None,
        },
        "show": {
            "global": None,
            "project": None,
            "session": None,
            "summary": None,
            "turns": None,
        },
    },
    "/model": None,
    "/plan": None,
    "/quit": None,
    "/reset": None,
    "/resume": None,
    "/sessions": None,
    "/summary": None,
    "/tools": None,
}


@dataclass(slots=True)
class InteractiveChatState:
    """Mutable interactive state backed by a persistent brain session."""

    session: BrainSession
    last_result: OrchestrationResult | None = None

    @property
    def session_id(self) -> str:
        """Return the stable session id."""
        return self.session.session_id

    @property
    def initial_cwd(self) -> Path:
        """Return the initial cwd recorded for this session."""
        return Path(self.session.initial_cwd)

    @property
    def current_cwd(self) -> Path:
        """Return the active cwd for this interactive shell."""
        return Path(self.session.current_cwd)

    @current_cwd.setter
    def current_cwd(self, value: Path) -> None:
        self.session.current_cwd = str(value)

    @property
    def approval_mode(self) -> ApprovalMode:
        """Return the current approval mode."""
        return ApprovalMode(self.session.approval_mode)

    @approval_mode.setter
    def approval_mode(self, value: ApprovalMode) -> None:
        self.session.approval_mode = value.value

    @property
    def model(self) -> str:
        """Return the persisted model override for this session."""
        return self.session.model

    @model.setter
    def model(self, value: str) -> None:
        self.session.model = value

    @property
    def provider_name(self) -> str:
        """Return the provider recorded for this session."""
        return self.session.provider_name

    @property
    def transcript(self) -> list[ProviderMessage]:
        """Return the bounded recent turn window."""
        return self.session.recent_turns

    @transcript.setter
    def transcript(self, value: list[ProviderMessage]) -> None:
        self.session.recent_turns = value

    @property
    def summary_text(self) -> str:
        """Return the compacted summary for older turns."""
        return self.session.summary_text

    @property
    def recovered_from_interruption(self) -> bool:
        """Return whether resume restored the last clean checkpoint."""
        return self.session.recovered_from_interruption

    @property
    def interrupted_turn(self) -> str | None:
        """Return the interrupted user input when the prior run did not finish cleanly."""
        return self.session.interrupted_turn


def _chat_history_path(settings: AppSettings) -> Path:
    history_path = settings.app.state_dir / _REPL_HISTORY_FILENAME
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return history_path


def _settings_for_interactive_session(
    settings: AppSettings,
    state: InteractiveChatState,
) -> AppSettings:
    runtime_settings = settings.model_copy(deep=True)
    runtime_settings.provider.name = state.provider_name
    runtime_settings.provider.model = state.model
    return runtime_settings


def _build_chat_prompt_session(settings: AppSettings) -> Any:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import NestedCompleter
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError as exc:
        raise RuntimeError(
            "Interactive chat requires prompt_toolkit. Reinstall dependencies with "
            "`./scripts/uv sync --extra dev`."
        ) from exc

    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit_input(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _insert_multiline_newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(_chat_history_path(settings))),
        auto_suggest=AutoSuggestFromHistory(),
        completer=NestedCompleter.from_nested_dict(_REPL_COMMAND_COMPLETIONS),
        complete_while_typing=True,
        key_bindings=bindings,
        multiline=True,
        reserve_space_for_menu=6,
    )


def _resolve_chat_session_cwd(workspace_root: Path, cwd: Path | None) -> Path:
    resolved_workspace_root = workspace_root.resolve()
    resolved = _resolve_cli_request_cwd(resolved_workspace_root, cwd)
    try:
        resolved.relative_to(resolved_workspace_root)
    except ValueError as exc:
        raise ValueError(
            "Interactive chat cwd must stay within the configured workspace root."
        ) from exc
    if not resolved.exists():
        raise ValueError(f"Interactive chat cwd does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Interactive chat cwd is not a directory: {resolved}")
    return resolved


def _format_repl_cwd(workspace_root: Path, cwd: Path) -> str:
    try:
        relative = cwd.relative_to(workspace_root)
    except ValueError:
        return str(cwd)
    return "." if str(relative) == "." else str(relative)


def _chat_prompt(state: InteractiveChatState, *, settings: AppSettings, plan_only: bool) -> str:
    mode_suffix = " plan-only" if plan_only else ""
    session_id = state.session_id[:8]
    return (
        f"foundation[{session_id} {_format_repl_cwd(settings.workspace_root, state.current_cwd)} "
        f"{state.approval_mode.value} {state.model}{mode_suffix}]> "
    )


def _render_interactive_chat_help() -> None:
    help_text = (
        "Natural-language request: send it directly\n"
        f"Direct shell command: prefix with `{_REPL_SHELL_PREFIX}`\n"
        "Submit input: press Enter\n"
        "Insert newline: press Esc then Enter\n"
        "/help: show this help\n"
        "/history [limit]: list recent persisted sessions\n"
        "/sessions [limit]: list persistent chat sessions\n"
        "/resume [session-id|latest]: switch to another persistent session\n"
        "/plan: inspect the last structured plan\n"
        "/actions: inspect the last action results\n"
        "/summary: inspect the last orchestration summary\n"
        "/memory: show loaded memory layers\n"
        "/memory show [global|project|session|summary|turns]: inspect one memory source\n"
        "/memory append [global|project] <text>: append to a memory file\n"
        "/memory set [global|project] <text>: replace a memory file\n"
        "/compact: compact older turns into the session summary now\n"
        "/model [name]: show or change the session model\n"
        "/tools: show current local tool availability\n"
        "/config [locations]: inspect effective config or paths\n"
        "/cwd [path]: show or update the interactive working directory\n"
        "/approval [auto|manual|prompt]: inspect or change session approval mode\n"
        "/clear: clear the terminal and redraw the session header\n"
        "/reset: reset session-local cwd, approval mode, model, and transcript state\n"
        "/exit or /quit: leave interactive chat"
    )
    console.print(
        Panel.fit(
            Text(help_text),
            title="Interactive Chat Help",
        )
    )


def _memory_source_label(source: MemorySource) -> str:
    labels = {
        MemorySource.GLOBAL: "Global user memory",
        MemorySource.PROJECT: "Project memory",
        MemorySource.SESSION_SUMMARY: "Session summary",
        MemorySource.RECENT_TURNS: "Recent turns",
    }
    return labels[source]


def _loaded_memory_labels(envelope: MemoryEnvelope) -> list[str]:
    return [
        layer.label
        for layer in envelope.layers
        if layer.content.strip() or (layer.path is not None and layer.exists)
    ]


def _render_interactive_chat_welcome(
    settings: AppSettings,
    state: InteractiveChatState,
    *,
    plan_only: bool,
    render_mode: RenderMode,
    memory_envelope: MemoryEnvelope,
) -> None:
    plan_only_text = "enabled" if plan_only else "disabled"
    loaded_memories = ", ".join(_loaded_memory_labels(memory_envelope)) or "none"
    console.print(
        Panel.fit(
            f"Session: [cyan]{escape(state.session_id)}[/cyan]\n"
            f"Workspace root: [cyan]{escape(str(settings.workspace_root))}[/cyan]\n"
            f"Request cwd: [cyan]{escape(str(state.current_cwd))}[/cyan]\n"
            f"Approval mode: [cyan]{state.approval_mode.value}[/cyan]\n"
            f"Provider: [cyan]{escape(state.provider_name)}[/cyan]\n"
            f"Model: [cyan]{escape(state.model)}[/cyan]\n"
            f"Render mode: [cyan]{render_mode.value}[/cyan]\n"
            f"Plan-only default: [cyan]{plan_only_text}[/cyan]\n"
            f"Loaded memory: [cyan]{escape(loaded_memories)}[/cyan]\n"
            f"Prompt history: [cyan]{escape(str(_chat_history_path(settings)))}[/cyan]\n\n"
            "Enter a request to use the planner, prefix with `!` for a direct shell command, "
            "or use `/plan`, `/actions`, `/summary`, or `/help` for session commands.",
            title="Interactive Chat",
        )
    )
    if state.recovered_from_interruption:
        interrupted_turn = state.interrupted_turn or "unknown input"
        console.print(
            Text(
                (
                    "Recovered the last clean checkpoint after an interrupted turn: "
                    f"{interrupted_turn}"
                ),
                style="yellow",
            )
        )


def _render_memory_envelope(envelope: MemoryEnvelope) -> None:
    table = Table(title="Session Memory")
    table.add_column("Source", style="cyan")
    table.add_column("Location")
    table.add_column("Loaded", justify="center")
    for layer in envelope.layers:
        table.add_row(
            layer.label,
            layer.path or "-",
            "yes" if layer.content.strip() else "no",
        )
    console.print(table)


def _render_memory_layer(layer: MemoryLayer) -> None:
    title = layer.label if layer.path is None else f"{layer.label}: {layer.path}"
    content = layer.content or "[empty]"
    console.print(Panel.fit(content, title=title))


def _memory_layer_for_source(envelope: MemoryEnvelope, source: MemorySource) -> MemoryLayer:
    for layer in envelope.layers:
        if layer.source is source:
            return layer
    raise ValueError(f"Memory source {source.value!r} is not available.")


def _render_brain_sessions(sessions: list[SessionSnapshot]) -> None:
    if not sessions:
        console.print("No persistent chat sessions recorded yet.")
        return
    table = Table(title="Chat Sessions")
    table.add_column("Session", style="cyan")
    table.add_column("Updated")
    table.add_column("Turns", justify="right")
    table.add_column("Cwd")
    table.add_column("Model")
    table.add_column("State")
    for session in sessions:
        state = "interrupted" if session.recovered_from_interruption else "ready"
        table.add_row(
            session.session_id,
            session.updated_at,
            str(session.turn_count),
            session.current_cwd,
            session.model,
            state,
        )
    console.print(table)


def _parse_memory_source(argument: str) -> MemorySource:
    normalized = argument.strip().lower()
    aliases = {
        "global": MemorySource.GLOBAL,
        "project": MemorySource.PROJECT,
        "session": MemorySource.SESSION_SUMMARY,
        "summary": MemorySource.SESSION_SUMMARY,
        "turns": MemorySource.RECENT_TURNS,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(aliases))
        raise ValueError(f"Expected one of: {choices}.") from exc


def _record_repl_shell_session(
    history_store: HistoryStore,
    session_id: str,
    *,
    request: ShellCommandRequest,
    request_cwd: Path,
    decision: PolicyDecision,
    result: ShellCommandResult | None,
    execution_status: str,
    summary_text: str,
    error: str | None,
) -> None:
    history_store.record_command(
        session_id,
        action_id=None,
        source="chat.repl",
        command=request.command,
        args=list(request.args),
        cwd=str(result.cwd if result is not None else request_cwd),
        mode=result.mode.value if result is not None else request.mode.value,
        policy_decision=decision.decision.value,
        policy_reason=decision.reason,
        risk_categories=list(decision.risk_categories),
        execution_status=execution_status,
        exit_code=None if result is None else result.exit_code,
        duration_seconds=None if result is None else result.duration_seconds,
        stdout="" if result is None else result.stdout,
        stderr="" if result is None else result.stderr,
        stdout_truncated=False if result is None else result.stdout_truncated,
        stderr_truncated=False if result is None else result.stderr_truncated,
        error=error,
    )
    history_store.record_summary(
        session_id,
        assistant_message=None,
        summary_text=summary_text,
        executed_actions=1 if execution_status == "executed" else 0,
        pending_approval_actions=1 if execution_status == "pending_approval" else 0,
        blocked_actions=1 if execution_status == "blocked" else 0,
        failed_actions=1 if execution_status == "failed" else 0,
        skipped_actions=0,
    )
    history_store.finalize_session(
        session_id,
        status=(
            SessionStatus.FAILED
            if execution_status == "failed"
            else (
                SessionStatus.PENDING_APPROVAL
                if execution_status == "pending_approval"
                else SessionStatus.COMPLETED
            )
        ),
    )


def _execute_repl_shell_command(
    *,
    raw_command: str,
    settings: AppSettings,
    state: InteractiveChatState,
    session_manager: SessionManager,
    history_store: HistoryStore,
    shell_runtime: ShellRuntime,
    policy_engine: GuardrailPolicyEngine,
) -> None:
    request_id = f"req-{uuid.uuid4().hex}"

    def _append_shell_transcript(
        *,
        summary_text: str,
        execution_status: str,
        result: ShellCommandResult | None = None,
        error: str | None = None,
    ) -> None:
        lines = [
            f"Direct shell command: `{shlex.join(request.argv)}`",
            f"Status: {execution_status}",
            f"Outcome: {summary_text}",
        ]
        if result is not None:
            stdout_preview = _preview_transcript_text(result.stdout)
            stderr_preview = _preview_transcript_text(result.stderr)
            if stdout_preview:
                lines.append(f"stdout:\n{stdout_preview}")
            if stderr_preview:
                lines.append(f"stderr:\n{stderr_preview}")
        elif error:
            lines.append(f"Error: {error}")
        session_manager.record_turn(
            state.session,
            turn_kind="shell",
            user_message=f"{_REPL_SHELL_PREFIX}{raw_command}",
            assistant_message="\n".join(lines),
            metadata={"execution_status": execution_status},
        )

    try:
        argv = shlex.split(raw_command)
    except ValueError as exc:
        console.print(f"[bold red]Shell parse error:[/bold red] {exc}")
        return
    if not argv:
        console.print(Text("No shell command was supplied after '!'.", style="dim"))
        return

    session_manager.mark_turn_started(
        state.session,
        user_message=f"{_REPL_SHELL_PREFIX}{raw_command}",
        turn_kind="shell",
    )

    request_cwd = state.current_cwd
    request = ShellCommandRequest(
        command=argv[0],
        args=argv[1:],
        cwd=request_cwd,
        approval_context={"source": "chat.repl", "request_id": request_id},
        mode=ExecutionMode.STREAM,
    )
    action = PlannedAction(
        id="repl_shell",
        kind=ActionKind.SHELL,
        summary=f"Run `{shlex.join(request.argv)}` from the interactive session.",
        shell=ShellAction(
            command=request.command,
            args=request.args,
            cwd=str(request_cwd),
            mode=ShellActionMode.STREAM,
        ),
    )
    evaluation = policy_engine.evaluate(
        action,
        request_cwd=request_cwd,
        approval_mode=state.approval_mode,
    )
    decision = (
        policy_engine.to_policy_decision(evaluation)
        if evaluation is not None
        else PolicyDecision(
            action_id=action.id,
            decision=PolicyDecisionType.ALLOW,
            reason="Explanation-only actions do not execute anything.",
        )
    )
    session_id = history_store.start_session(
        kind=SessionKind.RUN,
        workspace_root=settings.workspace_root,
        request_cwd=request_cwd,
        approval_mode=state.approval_mode.value,
        command_preview=shlex.join(request.argv),
    )
    if evaluation is not None:
        history_store.record_policy_evaluation(session_id, record=evaluation)

    if decision.decision is PolicyDecisionType.BLOCK:
        console.print(f"[bold red]Execution blocked:[/bold red] {decision.reason}")
        _record_repl_shell_session(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            decision=decision,
            result=None,
            execution_status="blocked",
            summary_text=decision.reason,
            error=decision.reason,
        )
        _append_shell_transcript(
            summary_text=decision.reason,
            execution_status="blocked",
            error=decision.reason,
        )
        return

    if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL:
        if evaluation is None:
            console.print("[bold red]Execution blocked:[/bold red] Missing policy evaluation.")
            return
        approval_request, approval_resolution = ApprovalService(
            mode=state.approval_mode,
            prompt_callback=_prompt_for_approval,
        ).resolve(
            action,
            evaluation,
            request_cwd=request_cwd,
        )
        history_store.record_approval(
            session_id,
            request=approval_request,
            resolution=approval_resolution,
        )
        if approval_resolution.status is ApprovalDecisionStatus.PENDING:
            console.print(
                f"[bold yellow]Approval pending:[/bold yellow] {approval_resolution.reason}"
            )
            _record_repl_shell_session(
                history_store,
                session_id,
                request=request,
                request_cwd=request_cwd,
                decision=decision,
                result=None,
                execution_status="pending_approval",
                summary_text=approval_resolution.reason,
                error=None,
            )
            _append_shell_transcript(
                summary_text=approval_resolution.reason,
                execution_status="pending_approval",
            )
            return
        if approval_resolution.status is ApprovalDecisionStatus.DENIED:
            console.print(f"[bold red]Execution blocked:[/bold red] {approval_resolution.reason}")
            _record_repl_shell_session(
                history_store,
                session_id,
                request=request,
                request_cwd=request_cwd,
                decision=decision,
                result=None,
                execution_status="blocked",
                summary_text=approval_resolution.reason,
                error=approval_resolution.reason,
            )
            _append_shell_transcript(
                summary_text=approval_resolution.reason,
                execution_status="blocked",
                error=approval_resolution.reason,
            )
            return

    if evaluation is not None:
        policy_engine.register_invocation(evaluation)

    effective_request = request
    if evaluation is not None:
        budget = (
            evaluation.verdict.constraints or evaluation.policy_input.constraints
        ).invocation_budget
        if budget is not None:
            timeout_seconds = request.timeout_seconds
            if budget.timeout_seconds is not None and timeout_seconds is not None:
                timeout_seconds = min(timeout_seconds, budget.timeout_seconds)
            effective_request = request.model_copy(
                update={
                    "timeout_seconds": timeout_seconds,
                    "capture_limit_kb": budget.output_limit_kb,
                }
            )

    try:
        result = shell_runtime.execute(effective_request, on_event=_emit_output_event)
    except ValueError as exc:
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        _record_repl_shell_session(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            decision=decision,
            result=None,
            execution_status="failed",
            summary_text=f"Shell execution was rejected: {exc}",
            error=str(exc),
        )
        _append_shell_transcript(
            summary_text=f"Shell execution was rejected: {exc}",
            execution_status="failed",
            error=str(exc),
        )
        return
    except ShellExecutionSpawnError as exc:
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        _record_repl_shell_session(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            decision=decision,
            result=None,
            execution_status="failed",
            summary_text=f"Shell execution failed to start: {exc}",
            error=str(exc),
        )
        _append_shell_transcript(
            summary_text=f"Shell execution failed to start: {exc}",
            execution_status="failed",
            error=str(exc),
        )
        return
    except ShellExecutionTimeout as exc:
        if exc.result is not None:
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        _record_repl_shell_session(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            decision=decision,
            result=exc.result,
            execution_status="failed",
            summary_text=f"Shell execution timed out: {exc}",
            error=str(exc),
        )
        _append_shell_transcript(
            summary_text=f"Shell execution timed out: {exc}",
            execution_status="failed",
            result=exc.result,
            error=str(exc),
        )
        return
    except ShellExecutionCancelled as exc:
        if exc.result is not None:
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution cancelled:[/bold red] {exc}")
        _record_repl_shell_session(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            decision=decision,
            result=exc.result,
            execution_status="failed",
            summary_text=f"Shell execution was cancelled: {exc}",
            error=str(exc),
        )
        _append_shell_transcript(
            summary_text=f"Shell execution was cancelled: {exc}",
            execution_status="failed",
            result=exc.result,
            error=str(exc),
        )
        return

    _render_execution_summary(result)
    execution_status = "executed" if result.ok else "failed"
    summary_text = (
        f"Executed shell command `{result.display_command}`."
        if result.ok
        else result.stderr or f"Shell command `{result.display_command}` failed."
    )
    _record_repl_shell_session(
        history_store,
        session_id,
        request=request,
        request_cwd=request_cwd,
        decision=decision,
        result=result,
        execution_status=execution_status,
        summary_text=summary_text,
        error=None if result.ok else result.stderr or f"Exit code {result.exit_code}",
    )
    _append_shell_transcript(
        summary_text=summary_text,
        execution_status=execution_status,
        result=result,
        error=None if result.ok else result.stderr or f"Exit code {result.exit_code}",
    )


def _build_transcript_assistant_message(result: OrchestrationResult) -> str:
    lines = [result.assistant_message.content, f"Outcome: {result.summary.text}"]
    for execution_result in result.execution_results:
        lines.append(
            f"{execution_result.action_id}: {execution_result.status.value} - "
            f"{execution_result.summary}"
        )
    return "\n".join(lines)


def _append_chat_transcript_turn(
    session_manager: SessionManager,
    state: InteractiveChatState,
    *,
    user_message: str,
    result: OrchestrationResult,
) -> None:
    session_manager.record_turn(
        state.session,
        turn_kind="chat",
        user_message=user_message,
        assistant_message=_build_transcript_assistant_message(result),
        metadata={"history_session_id": result.session_id or ""},
    )


@dataclass(slots=True)
class InteractiveChatRunner:
    """Own the dependencies for one interactive chat loop."""

    settings: AppSettings
    initial_cwd: Path
    plan_only: bool
    render_mode: RenderMode
    resume_target: ResumeTarget
    disable_live_ux: bool = False
    disable_monitor: bool = False
    monitor_socket: str | None = None
    monitor_http_port: int | None = None

    def run(self) -> None:
        try:
            prompt_session = _build_chat_prompt_session(self.settings)
        except RuntimeError as exc:
            console.print(f"[bold red]Interactive chat error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        session_manager = _build_session_manager(self.settings)
        try:
            session = session_manager.resolve_session(
                self.resume_target,
                initial_cwd=self.initial_cwd,
                approval_mode=self.settings.approval.mode.value,
                model=self.settings.provider.model,
            )
        except ValueError as exc:
            console.print(f"[bold red]Interactive chat error:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc

        state = InteractiveChatState(session=session)
        history_store = _build_history_store(self.settings)
        shell_runtime = _build_shell_runtime(self.settings)
        policy_engine = GuardrailPolicyEngine(
            workspace_root=self.settings.workspace_root,
            capability_registry=_build_capability_registry(self.settings),
        )
        _render_interactive_chat_welcome(
            self.settings,
            state,
            plan_only=self.plan_only,
            render_mode=self.render_mode,
            memory_envelope=session_manager.build_memory_envelope(state.session),
        )

        # When a capability-gap handoff offers a constrained retry and the user
        # accepts, the chosen follow-up is queued here and run as the next turn
        # without re-prompting.
        pending_request: str | None = None

        while True:
            if pending_request is not None:
                raw_input = pending_request
                pending_request = None
                console.print(Text(f"↪ Continuing: {raw_input.splitlines()[0]}", style="dim"))
            else:
                try:
                    raw_input = prompt_session.prompt(
                        _chat_prompt(state, settings=self.settings, plan_only=self.plan_only)
                    )
                except KeyboardInterrupt:
                    console.print(
                        Text(
                            "Input cancelled. Use /exit or Ctrl-D to leave the session.",
                            style="dim",
                        )
                    )
                    continue
                except EOFError:
                    console.print(Text("Interactive chat closed.", style="dim"))
                    return

            text = raw_input.strip()
            if not text:
                continue
            command, _, argument = text.partition(" ")
            argument = argument.strip()

            if command in {"/exit", "/quit"}:
                console.print(Text("Interactive chat closed.", style="dim"))
                return
            if command == "/help":
                _render_interactive_chat_help()
                continue
            if command == "/history":
                try:
                    limit = _REPL_DEFAULT_HISTORY_LIMIT if not argument else int(argument)
                except ValueError:
                    console.print("[bold red]History error:[/bold red] Expected an integer limit.")
                    continue
                if limit <= 0:
                    console.print("[bold red]History error:[/bold red] Limit must be positive.")
                    continue
                _render_history_list(history_store.list_sessions(limit=limit))
                continue
            if command == "/sessions":
                try:
                    limit = _REPL_DEFAULT_HISTORY_LIMIT if not argument else int(argument)
                except ValueError:
                    console.print("[bold red]Session error:[/bold red] Expected an integer limit.")
                    continue
                if limit <= 0:
                    console.print("[bold red]Session error:[/bold red] Limit must be positive.")
                    continue
                _render_brain_sessions(session_manager.list_sessions(limit=limit))
                continue
            if command == "/resume":
                target = (
                    ResumeTarget.latest()
                    if not argument or argument == "latest"
                    else ResumeTarget.explicit(argument)
                )
                try:
                    state.session = session_manager.resolve_session(
                        target,
                        initial_cwd=self.initial_cwd,
                        approval_mode=self.settings.approval.mode.value,
                        model=self.settings.provider.model,
                    )
                except ValueError as exc:
                    console.print(f"[bold red]Resume error:[/bold red] {exc}")
                    continue
                state.last_result = None
                _render_interactive_chat_welcome(
                    self.settings,
                    state,
                    plan_only=self.plan_only,
                    render_mode=self.render_mode,
                    memory_envelope=session_manager.build_memory_envelope(state.session),
                )
                continue
            if command in {
                InteractiveDetailCommand.PLAN.value,
                InteractiveDetailCommand.ACTIONS.value,
                InteractiveDetailCommand.SUMMARY.value,
            }:
                if state.last_result is None:
                    console.print(
                        Text(
                            "No assistant turn has completed in this session yet.",
                            style="dim",
                        )
                    )
                    continue
                _render_interactive_detail(state.last_result, InteractiveDetailCommand(command))
                continue
            if command == "/memory":
                if not argument:
                    _render_memory_envelope(session_manager.build_memory_envelope(state.session))
                    continue
                subcommand, _, remainder = argument.partition(" ")
                subcommand = subcommand.strip().lower()
                remainder = remainder.strip()
                if subcommand == "show":
                    if not remainder:
                        console.print(
                            "[bold red]Memory error:[/bold red] Expected a memory source to show."
                        )
                        continue
                    try:
                        source = _parse_memory_source(remainder)
                        envelope = session_manager.build_memory_envelope(state.session)
                        _render_memory_layer(_memory_layer_for_source(envelope, source))
                    except ValueError as exc:
                        console.print(f"[bold red]Memory error:[/bold red] {exc}")
                    continue
                if subcommand in {"append", "set"}:
                    source_text, _, content = remainder.partition(" ")
                    if not source_text or not content.strip():
                        console.print(
                            "[bold red]Memory error:[/bold red] Expected "
                            "`/memory append|set [global|project] <text>`."
                        )
                        continue
                    try:
                        layer = session_manager.write_memory(
                            _parse_memory_source(source_text),
                            content=content,
                            append=subcommand == "append",
                        )
                    except ValueError as exc:
                        console.print(f"[bold red]Memory error:[/bold red] {exc}")
                        continue
                    console.print(Text(f"{layer.label} updated.", style="dim"))
                    continue
                console.print(
                    "[bold red]Memory error:[/bold red] Use `/memory`, "
                    "`/memory show ...`, `/memory append ...`, or `/memory set ...`."
                )
                continue
            if command == "/compact":
                changed = session_manager.compact_session(state.session, force=True)
                if changed:
                    console.print(Text("Session memory compacted.", style="dim"))
                else:
                    console.print(Text("No compaction was needed.", style="dim"))
                continue
            if command == "/model":
                if not argument:
                    console.print(Text(f"Current model: {state.model}", style="dim"))
                    continue
                state.model = argument
                session_manager.checkpoint(state.session)
                console.print(Text(f"Interactive model set to {state.model}.", style="dim"))
                continue
            if command == "/tools":
                _render_availability(_build_tool_service(self.settings).availability_report())
                continue
            if command == "/config":
                if argument and argument != "locations":
                    console.print(
                        "[bold red]Config error:[/bold red] Use `/config` or `/config locations`."
                    )
                    continue
                payload = (
                    self.settings.config_locations()
                    if argument == "locations"
                    else render_settings_payload(self.settings)
                )
                console.print_json(data=payload)
                continue
            if command == "/cwd":
                if not argument:
                    console.print(Text(f"Current cwd: {state.current_cwd}", style="dim"))
                    continue
                try:
                    state.current_cwd = _resolve_chat_session_cwd(
                        self.settings.workspace_root,
                        Path(argument),
                    )
                except ValueError as exc:
                    console.print(f"[bold red]Cwd error:[/bold red] {exc}")
                    continue
                session_manager.checkpoint(state.session)
                console.print(Text(f"Interactive cwd set to {state.current_cwd}.", style="dim"))
                continue
            if command == "/approval":
                argument = argument.lower()
                if not argument:
                    console.print(
                        Text(f"Current approval mode: {state.approval_mode.value}", style="dim")
                    )
                    continue
                try:
                    state.approval_mode = ApprovalMode(argument)
                except ValueError:
                    choices = ", ".join(mode.value for mode in ApprovalMode)
                    console.print(
                        f"[bold red]Approval error:[/bold red] Expected one of: {choices}."
                    )
                    continue
                session_manager.checkpoint(state.session)
                console.print(
                    Text(
                        f"Interactive approval mode set to {state.approval_mode.value}.",
                        style="dim",
                    )
                )
                continue
            if command == "/clear":
                console.clear()
                _render_interactive_chat_welcome(
                    self.settings,
                    state,
                    plan_only=self.plan_only,
                    render_mode=self.render_mode,
                    memory_envelope=session_manager.build_memory_envelope(state.session),
                )
                continue
            if command == "/reset":
                session_manager.reset_session(
                    state.session,
                    current_cwd=state.initial_cwd,
                    approval_mode=self.settings.approval.mode.value,
                    model=self.settings.provider.model,
                )
                state.last_result = None
                console.print(Text("Session-local state reset.", style="dim"))
                continue
            if command.startswith("/"):
                console.print(f"[bold yellow]Unknown command:[/bold yellow] {text}. Use `/help`.")
                continue
            if text.startswith(_REPL_SHELL_PREFIX):
                _execute_repl_shell_command(
                    raw_command=text.removeprefix(_REPL_SHELL_PREFIX).strip(),
                    settings=self.settings,
                    state=state,
                    session_manager=session_manager,
                    history_store=history_store,
                    shell_runtime=shell_runtime,
                    policy_engine=policy_engine,
                )
                continue

            session_manager.mark_turn_started(
                state.session,
                user_message=text,
                turn_kind="chat",
            )
            memory_envelope = session_manager.build_memory_envelope(state.session)
            try:
                result = _execute_chat_request(
                    _settings_for_interactive_session(self.settings, state),
                    message=text,
                    conversation_history=memory_envelope.prompt_messages,
                    cwd=state.current_cwd,
                    plan_only=self.plan_only,
                    approval_mode=state.approval_mode,
                    shell_output_callback=_emit_output_event,
                    disable_live_ux=self.disable_live_ux,
                    disable_monitor=self.disable_monitor,
                    monitor_socket=self.monitor_socket,
                    monitor_http_port=self.monitor_http_port,
                )
            except ProviderError as exc:
                console.print(f"[bold red]Provider error:[/bold red] {exc}")
                session_manager.record_turn(
                    state.session,
                    turn_kind="chat",
                    user_message=text,
                    assistant_message=f"Provider error: {exc}",
                    metadata={"error_type": exc.__class__.__name__, "status": "failed"},
                )
                continue
            except OrchestrationPlanError as exc:
                console.print(f"[bold red]Planning error:[/bold red] {exc}")
                session_manager.record_turn(
                    state.session,
                    turn_kind="chat",
                    user_message=text,
                    assistant_message=f"Planning error: {exc}",
                    metadata={"error_type": exc.__class__.__name__, "status": "failed"},
                )
                continue
            except OrchestrationError as exc:
                console.print(f"[bold red]Orchestration error:[/bold red] {exc}")
                session_manager.record_turn(
                    state.session,
                    turn_kind="chat",
                    user_message=text,
                    assistant_message=f"Orchestration error: {exc}",
                    metadata={"error_type": exc.__class__.__name__, "status": "failed"},
                )
                continue

            state.last_result = result
            _render_chat_turn(
                result,
                render_mode=self.render_mode,
                interactive=True,
                streamed_shell_output=True,
            )
            _append_chat_transcript_turn(
                session_manager,
                state,
                user_message=text,
                result=result,
            )

            if result.gap_handoff is not None:
                pending_request = _handle_gap_handoff(result.gap_handoff, settings=self.settings)


def _run_interactive_chat(
    settings: AppSettings,
    *,
    initial_cwd: Path,
    plan_only: bool,
    render_mode: RenderMode,
    resume_target: ResumeTarget,
    disable_live_ux: bool = False,
    disable_monitor: bool = False,
    monitor_socket: str | None = None,
    monitor_http_port: int | None = None,
) -> None:
    InteractiveChatRunner(
        settings=settings,
        initial_cwd=initial_cwd,
        plan_only=plan_only,
        render_mode=render_mode,
        resume_target=resume_target,
        disable_live_ux=disable_live_ux,
        disable_monitor=disable_monitor,
        monitor_socket=monitor_socket,
        monitor_http_port=monitor_http_port,
    ).run()
