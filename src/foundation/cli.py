"""Typer application entrypoint for Foundation CLI."""

from __future__ import annotations

import logging
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from foundation import __version__
from foundation.doctor import DoctorReport, DoctorStatus, run_doctor
from foundation.logging import configure_logging
from foundation.models import (
    ActionKind,
    ApprovalDecisionStatus,
    ApprovalRequest,
    AuditDetailRef,
    AuditReport,
    BrainSession,
    ChatNotice,
    ChatSurfacePolicy,
    ChatTurnPresentation,
    ExecutionArtifactType,
    ExecutionResult,
    HistorySessionDetail,
    HistorySessionSummary,
    InteractiveDetailCommand,
    MemoryEnvelope,
    MemoryLayer,
    MemorySource,
    OrchestrationResult,
    PlannedAction,
    PolicyDecision,
    PolicyDecisionType,
    PresentationNoticeLevel,
    ProviderMessage,
    RenderMode,
    ResumeTarget,
    SessionKind,
    SessionSnapshot,
    SessionStatus,
    ShellAction,
    ShellActionMode,
    TerminalLogRouting,
    TraceQuery,
    TraceRecord,
    TraceSummary,
    UserRequest,
)
from foundation.services import (
    ApprovalService,
    CapabilityRegistry,
    CapabilityStore,
    ExecutionMode,
    FileDiscoveryRequest,
    FileDiscoveryResult,
    FileDiscoveryType,
    GitContextRequest,
    GitContextResult,
    GuardrailPolicyEngine,
    HelpLookupRequest,
    HelpLookupResult,
    HelpLookupSource,
    HistoryStore,
    LocalToolService,
    OrchestrationError,
    OrchestrationPlanError,
    OutputStream,
    ProviderError,
    RequestOrchestrator,
    SearchRequest,
    SearchResult,
    SessionManager,
    ShellCommandRequest,
    ShellCommandResult,
    ShellExecutionCancelled,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ShellOutputEvent,
    ShellRuntime,
    ToolAvailabilityStatus,
    ToolBinaryStatus,
    ToolErrorCode,
    ToolExecutionError,
    TraceStore,
    build_provider_adapter,
)
from foundation.settings import (
    ApprovalMode,
    AppSettings,
    LogLevel,
    SettingsLoadError,
    load_settings,
    render_settings_payload,
)

app = typer.Typer(
    name="foundation",
    help="Foundation CLI is a local-first shell-native assistant.",
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
)
config_app = typer.Typer(
    help="Inspect and validate Foundation CLI configuration.",
    no_args_is_help=False,
    invoke_without_command=True,
)
tool_app = typer.Typer(
    help="Inspect local workspace context through typed tool wrappers.",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(config_app, name="config")
app.add_typer(tool_app, name="tools")

console = Console()
stderr_console = Console(stderr=True)
logger = logging.getLogger("foundation.cli")

_REPL_DEFAULT_HISTORY_LIMIT = 10
_REPL_HISTORY_FILENAME = "repl-history.txt"
_REPL_SESSION_DB_FILENAME = "chat-sessions.sqlite3"
_REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS = 1200
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
class CLIContext:
    """Global CLI options that participate in settings resolution."""

    config_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


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


def _nested_override(root: dict[str, Any], dotted_path: str, value: Any) -> None:
    target = root
    path_parts = dotted_path.split(".")
    for part in path_parts[:-1]:
        target = target.setdefault(part, {})
    target[path_parts[-1]] = value


def _render_placeholder(command_name: str, detail: str, settings: AppSettings) -> None:
    console.print(
        Panel.fit(
            f"[bold]{command_name}[/bold]\n\n"
            f"{detail}\n\n"
            f"Workspace root: [cyan]{settings.workspace_root}[/cyan]\n"
            f"Config path: [cyan]{settings.config_path}[/cyan]\n"
            f"Approval mode: [cyan]{settings.approval.mode.value}[/cyan]",
            title="Foundation CLI",
        )
    )


def _load_runtime_settings(ctx: typer.Context) -> AppSettings:
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else CLIContext()

    try:
        settings = load_settings(
            config_path=cli_context.config_path,
            overrides=cli_context.overrides,
        )
    except SettingsLoadError as exc:
        logger.error("settings_load_failed: %s", exc)
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    configure_logging(
        settings.logging.level.value,
        log_path=settings.app.log_dir / "foundation.log",
        structured=settings.logging.structured,
        routing=(
            TerminalLogRouting.FILE_AND_TERMINAL
            if settings.logging.level is LogLevel.DEBUG
            else TerminalLogRouting.FILE_ONLY
        ),
    )
    logger.info(
        "settings_loaded command=%s config_path=%s config_exists=%s",
        ctx.command_path,
        settings.config_path,
        settings.config_exists,
    )
    return settings


def _render_doctor_report(report: DoctorReport) -> None:
    status_styles = {
        DoctorStatus.PASS: "green",
        DoctorStatus.WARN: "yellow",
        DoctorStatus.FAIL: "red",
    }

    for check in report.checks:
        style = status_styles[check.status]
        console.print(
            f"[{style}]{check.status.value.upper():<4}[/{style}] {check.name}: {check.summary}"
        )
        if check.detail:
            console.print(Text(check.detail, style="dim"))


def _build_shell_runtime(settings: AppSettings) -> ShellRuntime:
    return ShellRuntime(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=settings.shell.default_timeout_seconds,
        max_timeout_seconds=settings.shell.max_timeout_seconds,
        allow_pty=settings.shell.allow_pty,
        capture_limit_kb=settings.shell.capture_limit_kb,
        enforce_workspace_boundary=settings.shell.enforce_workspace_boundary,
    )


def _build_tool_service(settings: AppSettings) -> LocalToolService:
    return LocalToolService(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=min(settings.shell.default_timeout_seconds, 30),
        capture_limit_kb=settings.shell.capture_limit_kb,
    )


def _build_history_store(settings: AppSettings) -> HistoryStore:
    return TraceStore(
        database_path=settings.history.database_path,
        retention_days=settings.history.retention_days,
        max_entries=settings.history.max_entries,
    )


def _build_capability_registry(
    settings: AppSettings,
    *,
    tool_service: LocalToolService | None = None,
) -> CapabilityRegistry:
    service = tool_service or _build_tool_service(settings)
    return CapabilityRegistry(
        store=CapabilityStore(settings.app.data_dir / "capabilities"),
        tool_service=service,
    )


def _build_session_manager(settings: AppSettings) -> SessionManager:
    return SessionManager(
        database_path=settings.app.state_dir / _REPL_SESSION_DB_FILENAME,
        workspace_root=settings.workspace_root,
        config_dir=settings.config_path.parent,
        provider_name=settings.provider.name,
    )


def _prompt_for_approval(request: ApprovalRequest) -> bool:
    risk_text = ", ".join(request.risk_categories) if request.risk_categories else "unknown"
    lines = [
        f"Action: [cyan]{escape(request.action_id)}[/cyan]",
        (
            f"Capability: [cyan]{escape(request.capability_id)}[/cyan]"
            if request.capability_id
            else None
        ),
        f"Summary: {escape(request.summary)}",
        f"Reason: {escape(request.reason)}",
        f"Risk: [yellow]{escape(risk_text)}[/yellow]",
    ]
    if request.risk_class is not None:
        lines.append(f"Risk class: [yellow]{escape(request.risk_class.value)}[/yellow]")
    if request.trust_tier is not None:
        lines.append(f"Trust tier: [cyan]{escape(request.trust_tier.value)}[/cyan]")
    if request.command_preview:
        lines.append(f"Command: [cyan]{escape(request.command_preview)}[/cyan]")
    if request.cwd:
        lines.append(f"Cwd: [cyan]{escape(request.cwd)}[/cyan]")
    if request.paths:
        lines.append(f"Paths: [cyan]{escape(', '.join(request.paths))}[/cyan]")
    if request.network_hosts:
        lines.append(f"Network: [cyan]{escape(', '.join(request.network_hosts))}[/cyan]")
    if request.requested_side_effects:
        lines.append(
            "Side effects: "
            f"[yellow]{escape(', '.join(request.requested_side_effects))}[/yellow]"
        )
    if request.reason_codes:
        reason_text = ", ".join(code.value for code in request.reason_codes)
        lines.append(
            f"Policy reasons: [magenta]{escape(reason_text)}[/magenta]"
        )
    if request.constraints is not None and request.constraints.invocation_budget is not None:
        budget = request.constraints.invocation_budget
        budget_parts: list[str] = []
        if budget.timeout_seconds is not None:
            budget_parts.append(f"timeout={budget.timeout_seconds}s")
        if budget.output_limit_kb is not None:
            budget_parts.append(f"output={budget.output_limit_kb}KB")
        if budget.max_invocations is not None:
            budget_parts.append(f"max_invocations={budget.max_invocations}")
        if budget_parts:
            lines.append(f"Constraints: [cyan]{escape(', '.join(budget_parts))}[/cyan]")
    panel_text = "\n".join(item for item in lines if item is not None)
    console.print(Panel.fit(panel_text, title="Approval Required"))
    return typer.confirm("Approve this action?", default=False)


def _build_orchestrator(
    settings: AppSettings,
    *,
    approval_mode: ApprovalMode | None = None,
    shell_output_callback: Any | None = None,
) -> RequestOrchestrator:
    effective_approval_mode = approval_mode or settings.approval.mode
    tool_service = _build_tool_service(settings)
    return RequestOrchestrator(
        workspace_root=settings.workspace_root,
        approval_mode=effective_approval_mode,
        provider=build_provider_adapter(settings),
        shell_runtime=_build_shell_runtime(settings),
        tool_service=tool_service,
        approval_service=ApprovalService(
            mode=effective_approval_mode,
            prompt_callback=_prompt_for_approval,
        ),
        history_store=_build_history_store(settings),
        shell_output_callback=shell_output_callback,
        capability_registry=_build_capability_registry(settings, tool_service=tool_service),
    )


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


def _preview_transcript_text(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    if len(trimmed) <= _REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS:
        return trimmed
    return trimmed[:_REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS].rstrip() + "\n...[truncated]"


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


def _execute_chat_request(
    settings: AppSettings,
    *,
    message: str,
    conversation_history: list[ProviderMessage] | None = None,
    cwd: Path | None,
    plan_only: bool,
    approval_mode: ApprovalMode | None = None,
    shell_output_callback: Any | None = None,
) -> OrchestrationResult:
    orchestrator = _build_orchestrator(
        settings,
        approval_mode=approval_mode,
        shell_output_callback=shell_output_callback,
    )
    return orchestrator.orchestrate(
        UserRequest(
            message=message,
            conversation_history=list(conversation_history or []),
            cwd=cwd,
            plan_only=plan_only,
        )
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


def _run_interactive_chat(
    settings: AppSettings,
    *,
    initial_cwd: Path,
    plan_only: bool,
    render_mode: RenderMode,
    resume_target: ResumeTarget,
) -> None:
    try:
        prompt_session = _build_chat_prompt_session(settings)
    except RuntimeError as exc:
        console.print(f"[bold red]Interactive chat error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    session_manager = _build_session_manager(settings)
    try:
        session = session_manager.resolve_session(
            resume_target,
            initial_cwd=initial_cwd,
            approval_mode=settings.approval.mode.value,
            model=settings.provider.model,
        )
    except ValueError as exc:
        console.print(f"[bold red]Interactive chat error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    state = InteractiveChatState(session=session)
    history_store = _build_history_store(settings)
    shell_runtime = _build_shell_runtime(settings)
    policy_engine = GuardrailPolicyEngine(
        workspace_root=settings.workspace_root,
        capability_registry=_build_capability_registry(settings),
    )
    _render_interactive_chat_welcome(
        settings,
        state,
        plan_only=plan_only,
        render_mode=render_mode,
        memory_envelope=session_manager.build_memory_envelope(state.session),
    )

    while True:
        try:
            raw_input = prompt_session.prompt(
                _chat_prompt(state, settings=settings, plan_only=plan_only)
            )
        except KeyboardInterrupt:
            console.print(
                Text("Input cancelled. Use /exit or Ctrl-D to leave the session.", style="dim")
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
                    initial_cwd=initial_cwd,
                    approval_mode=settings.approval.mode.value,
                    model=settings.provider.model,
                )
            except ValueError as exc:
                console.print(f"[bold red]Resume error:[/bold red] {exc}")
                continue
            state.last_result = None
            _render_interactive_chat_welcome(
                settings,
                state,
                plan_only=plan_only,
                render_mode=render_mode,
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
            _render_availability(_build_tool_service(settings).availability_report())
            continue
        if command == "/config":
            if argument and argument != "locations":
                console.print(
                    "[bold red]Config error:[/bold red] Use `/config` or `/config locations`."
                )
                continue
            payload = (
                settings.config_locations()
                if argument == "locations"
                else render_settings_payload(settings)
            )
            console.print_json(data=payload)
            continue
        if command == "/cwd":
            if not argument:
                console.print(Text(f"Current cwd: {state.current_cwd}", style="dim"))
                continue
            try:
                state.current_cwd = _resolve_chat_session_cwd(
                    settings.workspace_root,
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
                console.print(f"[bold red]Approval error:[/bold red] Expected one of: {choices}.")
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
                settings,
                state,
                plan_only=plan_only,
                render_mode=render_mode,
                memory_envelope=session_manager.build_memory_envelope(state.session),
            )
            continue
        if command == "/reset":
            session_manager.reset_session(
                state.session,
                current_cwd=state.initial_cwd,
                approval_mode=settings.approval.mode.value,
                model=settings.provider.model,
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
                settings=settings,
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
                _settings_for_interactive_session(settings, state),
                message=text,
                conversation_history=memory_envelope.prompt_messages,
                cwd=state.current_cwd,
                plan_only=plan_only,
                approval_mode=state.approval_mode,
                shell_output_callback=_emit_output_event,
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
            render_mode=render_mode,
            interactive=True,
            streamed_shell_output=True,
        )
        _append_chat_transcript_turn(
            session_manager,
            state,
            user_message=text,
            result=result,
        )


def _parse_env_overlays(values: list[str]) -> dict[str, str]:
    overlay: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid --env value {item!r}. Expected NAME=VALUE.")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError("Environment variable names in --env cannot be empty.")
        overlay[key] = value
    return overlay


def _write_output(target: Console, text: str) -> None:
    if not text:
        return
    target.file.write(text)
    target.file.flush()


def _emit_output_event(event: ShellOutputEvent) -> None:
    target = stderr_console if event.stream is OutputStream.STDERR else console
    _write_output(target, event.text)


def _render_result_output(result: ShellCommandResult, *, streamed: bool) -> None:
    if streamed:
        return
    _write_output(console, result.stdout)
    _write_output(stderr_console, result.stderr)


def _format_result_status(result: ShellCommandResult) -> str:
    if result.timed_out:
        return "timed out"
    if result.cancelled:
        return "cancelled"
    if result.exit_code is None:
        return "unknown"
    if result.exit_code < 0:
        return f"signal {-result.exit_code}"
    return f"exit code {result.exit_code}"


def _render_execution_summary(result: ShellCommandResult) -> None:
    style = "green" if result.ok else "red"
    lines = [
        f"Command: [cyan]{escape(result.display_command)}[/cyan]",
        f"Cwd: [cyan]{escape(str(result.cwd))}[/cyan]",
        f"Mode: [cyan]{result.mode.value}[/cyan]",
        f"Status: [{style}]{escape(_format_result_status(result))}[/{style}]",
        f"Duration: [cyan]{result.duration_seconds:.2f}s[/cyan]",
    ]
    truncated_streams: list[str] = []
    if result.stdout_truncated:
        truncated_streams.append("stdout")
    if result.stderr_truncated:
        truncated_streams.append("stderr")
    if truncated_streams:
        lines.append(
            f"Captured output truncated: [yellow]{escape(', '.join(truncated_streams))}[/yellow]"
        )

    console.print(Panel.fit("\n".join(lines), title="Execution Summary"))


def _render_tool_error(exc: ToolExecutionError) -> None:
    console.print(f"[bold red]Tool error:[/bold red] {exc.error.message}")
    if exc.error.detail:
        console.print(Text(exc.error.detail, style="dim"))
    if exc.error.install_hint:
        console.print(Text(exc.error.install_hint, style="dim"))


def _tool_exit_code(exc: ToolExecutionError) -> int:
    if exc.error.code is ToolErrorCode.INVALID_SCOPE:
        return 2
    return 1


def _render_availability(availability: list[ToolBinaryStatus]) -> None:
    status_styles = {
        ToolAvailabilityStatus.AVAILABLE: "green",
        ToolAvailabilityStatus.MISSING: "yellow",
    }
    for item in availability:
        style = status_styles[item.status]
        resolved = f" ({item.resolved_command}: {item.path})" if item.path else ""
        required = "required" if item.required else "optional"
        console.print(
            f"[{style}]{item.status.value.upper():<9}[/{style}] {item.name}: {required}{resolved}"
        )
        if item.status is ToolAvailabilityStatus.MISSING and item.install_hint:
            console.print(Text(item.install_hint, style="dim"))


def _render_search_result(result: SearchResult) -> None:
    if not result.matches:
        console.print("No matches found.")
        return

    table = Table(title=f"Search Results ({result.scope})")
    table.add_column("Path", style="cyan")
    table.add_column("Line", justify="right")
    table.add_column("Col", justify="right")
    table.add_column("Text")
    for match in result.matches:
        table.add_row(
            match.path,
            str(match.line_number),
            str(match.column_number),
            match.line_text,
        )
    console.print(table)
    if result.truncated:
        console.print(Text("Search results were truncated to the requested limit.", style="dim"))


def _render_file_result(result: FileDiscoveryResult) -> None:
    if not result.paths:
        console.print("No paths found.")
        return

    table = Table(title=f"Path Discovery ({result.scope})")
    table.add_column("Path", style="cyan")
    for path in result.paths:
        table.add_row(path)
    console.print(table)
    if result.truncated:
        console.print(Text("Path results were truncated to the requested limit.", style="dim"))


def _render_git_context(result: GitContextResult) -> None:
    console.print(
        Panel.fit(
            f"Scope: [cyan]{result.scope}[/cyan]\nBranch: [cyan]{result.branch}[/cyan]",
            title="Git Context",
        )
    )

    if result.status:
        status_table = Table(title="Status")
        status_table.add_column("Index", justify="center")
        status_table.add_column("Worktree", justify="center")
        status_table.add_column("Path", style="cyan")
        for entry in result.status:
            status_table.add_row(entry.index_status, entry.worktree_status, entry.path)
        console.print(status_table)
        if result.truncated_status:
            console.print(Text("Status output was truncated to the requested limit.", style="dim"))

    if result.unstaged_diff:
        diff_table = Table(title="Unstaged Diff")
        diff_table.add_column("Path", style="cyan")
        diff_table.add_column("+", justify="right")
        diff_table.add_column("-", justify="right")
        diff_table.add_column("Binary", justify="center")
        for diff_entry in result.unstaged_diff:
            diff_table.add_row(
                diff_entry.path,
                "-" if diff_entry.additions is None else str(diff_entry.additions),
                "-" if diff_entry.deletions is None else str(diff_entry.deletions),
                "yes" if diff_entry.binary else "no",
            )
        console.print(diff_table)

    if result.staged_diff:
        diff_table = Table(title="Staged Diff")
        diff_table.add_column("Path", style="cyan")
        diff_table.add_column("+", justify="right")
        diff_table.add_column("-", justify="right")
        diff_table.add_column("Binary", justify="center")
        for diff_entry in result.staged_diff:
            diff_table.add_row(
                diff_entry.path,
                "-" if diff_entry.additions is None else str(diff_entry.additions),
                "-" if diff_entry.deletions is None else str(diff_entry.deletions),
                "yes" if diff_entry.binary else "no",
            )
        console.print(diff_table)

    if result.recent_commits:
        commit_table = Table(title="Recent Commits")
        commit_table.add_column("Commit", style="cyan")
        commit_table.add_column("Summary")
        for commit in result.recent_commits:
            commit_table.add_row(commit.short_sha, commit.summary)
        console.print(commit_table)


def _render_help_lookup(result: HelpLookupResult) -> None:
    console.print(
        Panel.fit(
            result.content or "No content returned.",
            title=f"{result.source.value}: {result.topic}",
        )
    )
    if result.truncated:
        console.print(Text("Help output was truncated to the requested limit.", style="dim"))


def _render_assistant_message(result: OrchestrationResult) -> None:
    console.print(Panel.fit(result.assistant_message.content, title="Assistant"))


def _chat_surface_policy(render_mode: RenderMode) -> ChatSurfacePolicy:
    return ChatSurfacePolicy(render_mode=render_mode)


def _has_hidden_chat_detail(result: OrchestrationResult) -> bool:
    return bool(result.plan.actions or result.execution_results or result.policy_decisions)


def _detail_ref_for_result(result: OrchestrationResult) -> AuditDetailRef | None:
    if result.session_id is None:
        return None
    return AuditDetailRef(
        session_id=result.session_id,
        history_hint=f"foundation history --session {result.session_id}",
        trace_hint=f"foundation trace --session {result.session_id}",
    )


def _notice_level_for_result(result: OrchestrationResult) -> PresentationNoticeLevel:
    if result.summary.failed_actions or result.summary.blocked_actions:
        return PresentationNoticeLevel.ERROR
    if result.summary.pending_approval_actions or result.request.plan_only:
        return PresentationNoticeLevel.WARNING
    if result.summary.executed_actions or result.summary.skipped_actions:
        return PresentationNoticeLevel.DIM
    return PresentationNoticeLevel.INFO


def _artifact_preview_notice(result: ExecutionResult) -> ChatNotice | None:
    if result.artifact_type is None or result.artifact is None:
        return None
    if result.artifact_type is ExecutionArtifactType.EXPLANATION:
        return None
    if result.artifact_type is ExecutionArtifactType.SHELL:
        shell_result = ShellCommandResult.model_validate(result.artifact)
        preview_lines: list[str] = []
        stdout_preview = _preview_transcript_text(shell_result.stdout)
        stderr_preview = _preview_transcript_text(shell_result.stderr)
        if stdout_preview:
            preview_lines.append(f"stdout:\n{stdout_preview}")
        if stderr_preview and not shell_result.ok:
            preview_lines.append(f"stderr:\n{stderr_preview}")
        if not preview_lines:
            return None
        return ChatNotice(
            level=PresentationNoticeLevel.DIM,
            text="\n".join(preview_lines),
        )
    if result.artifact_type is ExecutionArtifactType.SEARCH:
        search_result = SearchResult.model_validate(result.artifact)
        if not search_result.matches:
            return None
        preview_lines = [
            f"{match.path}:{match.line_number} {match.line_text}"
            for match in search_result.matches[:3]
        ]
        if len(search_result.matches) > 3 or search_result.truncated:
            preview_lines.append("...")
        return ChatNotice(
            level=PresentationNoticeLevel.DIM,
            text="Search preview:\n" + "\n".join(preview_lines),
        )
    if result.artifact_type is ExecutionArtifactType.FILES:
        file_result = FileDiscoveryResult.model_validate(result.artifact)
        if not file_result.paths:
            return None
        preview_lines = list(file_result.paths[:5])
        if len(file_result.paths) > 5 or file_result.truncated:
            preview_lines.append("...")
        return ChatNotice(
            level=PresentationNoticeLevel.DIM,
            text="Path preview:\n" + "\n".join(preview_lines),
        )
    if result.artifact_type is ExecutionArtifactType.GIT:
        git_result = GitContextResult.model_validate(result.artifact)
        preview_lines = [f"Branch: {git_result.branch}"]
        if git_result.status:
            changed_paths = [entry.path for entry in git_result.status[:5]]
            changed_text = ", ".join(changed_paths)
            if len(git_result.status) > 5 or git_result.truncated_status:
                changed_text += ", ..."
            preview_lines.append(f"Changed: {changed_text}")
        if git_result.recent_commits:
            preview_lines.append(
                "Recent: "
                + "; ".join(
                    f"{commit.short_sha} {commit.summary}"
                    for commit in git_result.recent_commits[:3]
                )
            )
        return ChatNotice(
            level=PresentationNoticeLevel.DIM,
            text="\n".join(preview_lines),
        )
    help_result = HelpLookupResult.model_validate(result.artifact)
    content_preview = _preview_transcript_text(help_result.content)
    if not content_preview:
        return None
    return ChatNotice(
        level=PresentationNoticeLevel.DIM,
        text=f"{help_result.source.value}: {help_result.topic}\n{content_preview}",
    )


def _build_chat_turn_presentation(
    result: OrchestrationResult,
    *,
    policy: ChatSurfacePolicy,
    interactive: bool,
) -> ChatTurnPresentation:
    primary_text = result.assistant_message.content.strip()
    notices: list[ChatNotice] = []
    explanation_messages = [
        str(item.artifact.get("message", "")).strip()
        for item in result.execution_results
        if item.artifact_type is ExecutionArtifactType.EXPLANATION and item.artifact is not None
    ]
    seen_messages = {primary_text}
    for message in explanation_messages:
        if message and message not in seen_messages:
            notices.append(
                ChatNotice(
                    level=PresentationNoticeLevel.DIM,
                    text=message,
                )
            )
            seen_messages.add(message)

    summary_text = result.summary.text.strip()
    if (result.plan.actions or result.execution_results) and summary_text not in seen_messages:
        notices.insert(
            0,
            ChatNotice(
                level=_notice_level_for_result(result),
                text=summary_text,
            ),
        )
        seen_messages.add(summary_text)

    for execution_result in result.execution_results:
        artifact_notice = _artifact_preview_notice(execution_result)
        if artifact_notice is not None and artifact_notice.text not in seen_messages:
            notices.append(artifact_notice)
            seen_messages.add(artifact_notice.text)

    audit_ref = None
    if policy.show_audit_refs and _has_hidden_chat_detail(result):
        audit_ref = _detail_ref_for_result(result)
        if audit_ref is not None:
            hint = (
                f"Session {audit_ref.session_id[:8]} saved. Use "
                f"{InteractiveDetailCommand.PLAN.value}, "
                f"{InteractiveDetailCommand.ACTIONS.value}, or "
                f"{InteractiveDetailCommand.SUMMARY.value} for detail."
                if interactive
                else (
                    f"Session {audit_ref.session_id[:8]} saved. Re-run with `--render verbose` "
                    f"or inspect with `{audit_ref.trace_hint}`."
                )
            )
            notices.append(
                ChatNotice(
                    level=PresentationNoticeLevel.DIM,
                    text=hint,
                )
            )

    return ChatTurnPresentation(
        primary_text=primary_text,
        notices=notices,
        audit_ref=audit_ref,
    )


def _render_action_plan(result: OrchestrationResult) -> None:
    if not result.plan.actions:
        console.print(Text("No actions planned.", style="dim"))
        return

    decisions = {decision.action_id: decision for decision in result.policy_decisions}
    evaluations = {record.action_id: record for record in result.policy_evaluations}
    table = Table(title="Planned Actions")
    table.add_column("Id", style="cyan")
    table.add_column("Kind")
    table.add_column("Summary")
    table.add_column("Policy")
    for action in result.plan.actions:
        decision = decisions.get(action.id)
        evaluation = evaluations.get(action.id)
        policy_text = "-"
        if evaluation is not None:
            policy_text = evaluation.verdict.outcome.value
        elif decision is not None:
            policy_text = decision.decision.value
        table.add_row(action.id, action.kind.value, action.summary, policy_text)
    console.print(table)


def _render_chat_execution_result(
    result: ExecutionResult,
    *,
    streamed_shell_output: bool = False,
) -> None:
    status_styles = {
        "executed": "green",
        "not_executed": "yellow",
        "pending_approval": "yellow",
        "blocked": "red",
        "failed": "red",
    }
    style = status_styles[result.status.value]
    console.print(
        Panel.fit(
            f"[{style}]{result.status.value}[/{style}]\n{escape(result.summary)}",
            title=f"Action {result.action_id}",
        )
    )
    if result.artifact_type is None or result.artifact is None:
        return

    if result.artifact_type is ExecutionArtifactType.SHELL:
        shell_result = ShellCommandResult.model_validate(result.artifact)
        _render_result_output(
            shell_result,
            streamed=(streamed_shell_output and shell_result.mode is not ExecutionMode.BUFFERED),
        )
        _render_execution_summary(shell_result)
        return
    if result.artifact_type is ExecutionArtifactType.SEARCH:
        _render_search_result(SearchResult.model_validate(result.artifact))
        return
    if result.artifact_type is ExecutionArtifactType.FILES:
        _render_file_result(FileDiscoveryResult.model_validate(result.artifact))
        return
    if result.artifact_type is ExecutionArtifactType.GIT:
        _render_git_context(GitContextResult.model_validate(result.artifact))
        return
    if result.artifact_type in {ExecutionArtifactType.MAN, ExecutionArtifactType.TLDR}:
        _render_help_lookup(HelpLookupResult.model_validate(result.artifact))
        return
    if result.artifact_type is ExecutionArtifactType.EXPLANATION:
        message = result.artifact.get("message", "")
        console.print(Text(str(message), style="dim"))


def _render_chat_execution_details(
    result: OrchestrationResult,
    *,
    streamed_shell_output: bool,
) -> None:
    for execution_result in result.execution_results:
        _render_chat_execution_result(
            execution_result,
            streamed_shell_output=streamed_shell_output,
        )


def _render_orchestration_summary(result: OrchestrationResult) -> None:
    usage = result.planning_metadata.usage
    usage_text = "unknown"
    if usage is not None and usage.total_tokens is not None:
        usage_text = str(usage.total_tokens)
    session_text = result.session_id or "not recorded"
    console.print(
        Panel.fit(
            f"{result.summary.text}\n\n"
            f"Session: [cyan]{session_text}[/cyan]\n"
            f"Provider: [cyan]{result.planning_metadata.provider}[/cyan]\n"
            f"Model: [cyan]{result.planning_metadata.model}[/cyan]\n"
            f"Latency: [cyan]{result.planning_metadata.latency_seconds:.2f}s[/cyan]\n"
            f"Attempts: [cyan]{result.planning_metadata.attempts}[/cyan]\n"
            f"Total tokens: [cyan]{usage_text}[/cyan]",
            title="Orchestration Summary",
        )
    )


def _render_concise_chat_turn(
    result: OrchestrationResult,
    *,
    policy: ChatSurfacePolicy,
    interactive: bool,
) -> None:
    presentation = _build_chat_turn_presentation(
        result,
        policy=policy,
        interactive=interactive,
    )
    console.print(presentation.primary_text)
    style_map = {
        PresentationNoticeLevel.INFO: "cyan",
        PresentationNoticeLevel.WARNING: "yellow",
        PresentationNoticeLevel.ERROR: "bold red",
        PresentationNoticeLevel.DIM: "dim",
    }
    for notice in presentation.notices:
        console.print(Text(notice.text, style=style_map[notice.level]))


def _render_verbose_chat_turn(
    result: OrchestrationResult,
    *,
    streamed_shell_output: bool,
) -> None:
    _render_assistant_message(result)
    _render_action_plan(result)
    _render_chat_execution_details(
        result,
        streamed_shell_output=streamed_shell_output,
    )
    _render_orchestration_summary(result)


def _render_chat_turn(
    result: OrchestrationResult,
    *,
    render_mode: RenderMode,
    interactive: bool = False,
    streamed_shell_output: bool = False,
) -> None:
    if render_mode is RenderMode.VERBOSE:
        _render_verbose_chat_turn(
            result,
            streamed_shell_output=streamed_shell_output,
        )
        return
    _render_concise_chat_turn(
        result,
        policy=_chat_surface_policy(render_mode),
        interactive=interactive,
    )


def _render_interactive_detail(
    result: OrchestrationResult,
    command: InteractiveDetailCommand,
) -> None:
    if command is InteractiveDetailCommand.PLAN:
        _render_action_plan(result)
        return
    if command is InteractiveDetailCommand.ACTIONS:
        if not result.execution_results:
            console.print(
                Text(
                    "No execution details were recorded for the last request.",
                    style="dim",
                )
            )
            return
        _render_chat_execution_details(result, streamed_shell_output=False)
        return
    _render_orchestration_summary(result)


def _render_history_list(sessions: list[HistorySessionSummary]) -> None:
    if not sessions:
        console.print("No history recorded yet.")
        return

    table = Table(title="Session History")
    table.add_column("Session", style="cyan")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Request")
    for session in sessions:
        request_text = session.request_text or session.command_preview or "-"
        table.add_row(
            session.session_id,
            session.kind.value,
            session.status.value,
            session.started_at,
            request_text,
        )
    console.print(table)


def _render_history_detail(session: HistorySessionDetail) -> None:
    console.print(
        Panel.fit(
            f"Session: [cyan]{session.session_id}[/cyan]\n"
            f"Kind: [cyan]{session.kind.value}[/cyan]\n"
            f"Status: [cyan]{session.status.value}[/cyan]\n"
            f"Started: [cyan]{session.started_at}[/cyan]\n"
            f"Request cwd: [cyan]{session.request_cwd}[/cyan]\n"
            f"Approval mode: [cyan]{session.approval_mode}[/cyan]",
            title="History Detail",
        )
    )
    if session.request_text:
        console.print(Panel.fit(session.request_text, title="User Request"))
    elif session.command_preview:
        console.print(Panel.fit(session.command_preview, title="Command"))

    if session.assistant_message:
        console.print(Panel.fit(session.assistant_message, title="Assistant"))

    if session.summary_text:
        console.print(
            Panel.fit(
                f"{session.summary_text}\n\n"
                f"Executed: [cyan]{session.executed_actions}[/cyan]\n"
                f"Pending approval: [cyan]{session.pending_approval_actions}[/cyan]\n"
                f"Blocked: [cyan]{session.blocked_actions}[/cyan]\n"
                f"Failed: [cyan]{session.failed_actions}[/cyan]\n"
                f"Skipped: [cyan]{session.skipped_actions}[/cyan]",
                title="Summary",
            )
        )

    if session.approvals:
        table = Table(title="Approvals")
        table.add_column("Action", style="cyan")
        table.add_column("Capability")
        table.add_column("Status")
        table.add_column("Mode")
        table.add_column("Reason")
        for approval in session.approvals:
            table.add_row(
                approval.action_id or "-",
                approval.capability_id or "-",
                approval.status.value,
                approval.mode,
                approval.reason,
            )
        console.print(table)

    if session.policy_evaluations:
        table = Table(title="Policy Evaluations")
        table.add_column("Action", style="cyan")
        table.add_column("Capability")
        table.add_column("Outcome")
        table.add_column("Reasons")
        for record in session.policy_evaluations:
            reasons = ", ".join(code.value for code in record.verdict.reason_codes) or "-"
            table.add_row(
                record.action_id,
                record.capability_id,
                record.verdict.outcome.value,
                reasons,
            )
        console.print(table)

    if session.tool_calls:
        table = Table(title="Tool Calls")
        table.add_column("Action", style="cyan")
        table.add_column("Tool")
        table.add_column("Status")
        table.add_column("Policy")
        for tool_call in session.tool_calls:
            table.add_row(
                tool_call.action_id,
                tool_call.tool,
                tool_call.execution_status,
                tool_call.policy_decision or "-",
            )
        console.print(table)

    if session.commands:
        table = Table(title="Commands")
        table.add_column("Action", style="cyan")
        table.add_column("Command")
        table.add_column("Status")
        table.add_column("Exit", justify="right")
        for command in session.commands:
            table.add_row(
                command.action_id or "-",
                shlex.join([command.command, *command.args]),
                command.execution_status,
                "-" if command.exit_code is None else str(command.exit_code),
            )
        console.print(table)


def _render_trace_list(traces: list[TraceSummary]) -> None:
    if not traces:
        console.print("No traces recorded yet.")
        return

    table = Table(title="Trace History")
    table.add_column("Trace", style="cyan")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Steps", justify="right")
    table.add_column("Capabilities")
    table.add_column("Request")
    for trace in traces:
        capabilities = ", ".join(trace.selected_capability_ids) or "-"
        table.add_row(
            trace.trace_id,
            trace.status.value,
            trace.started_at,
            str(trace.step_count),
            capabilities,
            trace.request_text or "-",
        )
    console.print(table)


def _render_trace_detail(trace: TraceRecord) -> None:
    console.print(
        Panel.fit(
            f"Trace: [cyan]{trace.trace_id}[/cyan]\n"
            f"Status: [cyan]{trace.status.value}[/cyan]\n"
            f"Started: [cyan]{trace.started_at}[/cyan]\n"
            f"Completed: [cyan]{trace.completed_at or '-'}[/cyan]\n"
            f"Steps: [cyan]{trace.summary.step_count}[/cyan]",
            title="Trace Detail",
        )
    )
    if trace.request_text:
        console.print(Panel.fit(trace.request_text, title="Request"))

    summary = trace.summary
    console.print(
        Panel.fit(
            f"Executed: [cyan]{summary.executed_steps}[/cyan]\n"
            f"Pending approval: [cyan]{summary.pending_approval_steps}[/cyan]\n"
            f"Blocked: [cyan]{summary.blocked_steps}[/cyan]\n"
            f"Failed: [cyan]{summary.failed_steps}[/cyan]\n"
            f"Skipped: [cyan]{summary.skipped_steps}[/cyan]",
            title="Trace Summary",
        )
    )

    if trace.edges:
        edge_table = Table(title="Trace Edges")
        edge_table.add_column("Source", style="cyan")
        edge_table.add_column("Target")
        edge_table.add_column("Kind")
        for edge in trace.edges:
            edge_table.add_row(edge.source_step_id, edge.target_step_id, edge.edge_kind.value)
        console.print(edge_table)

    if trace.steps:
        step_table = Table(title="Trace Steps")
        step_table.add_column("Step", style="cyan")
        step_table.add_column("Type")
        step_table.add_column("Status")
        step_table.add_column("Capability")
        step_table.add_column("Why")
        for step in trace.steps:
            if step.step_type.value == "planning":
                status = "planned"
                capability = "-"
                why = ", ".join(reason.summary for reason in step.selection_reasons) or "-"
            else:
                status = step.status.value
                capability = step.capability_id or "-"
                why = step.selection_reason.detail or step.selection_reason.summary
            step_table.add_row(
                step.step_id,
                step.step_type.value,
                status,
                capability,
                why,
            )
        console.print(step_table)


def _render_audit_report(report: AuditReport) -> None:
    status_text = "pass" if report.completeness_passed else "fail"
    console.print(
        Panel.fit(
            f"Trace: [cyan]{report.trace_summary.trace_id}[/cyan]\n"
            f"Completeness: [cyan]{status_text}[/cyan]\n"
            f"Inspected step: [cyan]{report.inspected_step_id or 'all'}[/cyan]",
            title="Audit Report",
        )
    )
    if report.notes:
        console.print(Panel.fit("\n".join(report.notes), title="Notes"))
    if report.missing_fields_by_step:
        table = Table(title="Missing Fields")
        table.add_column("Step", style="cyan")
        table.add_column("Fields")
        for step_id, fields in report.missing_fields_by_step.items():
            table.add_row(step_id, ", ".join(fields))
        console.print(table)
    _render_trace_detail(
        TraceRecord(
            trace_id=report.trace_summary.trace_id,
            session_id=report.trace_summary.session_id,
            request_text=report.trace_summary.request_text,
            status=report.trace_summary.status,
            started_at=report.trace_summary.started_at,
            completed_at=report.trace_summary.completed_at,
            steps=report.steps,
            edges=report.edges,
            summary=report.trace_summary,
        )
    )


def _resolve_cli_request_cwd(workspace_root: Path, cwd: Path | None) -> Path:
    if cwd is None:
        return workspace_root.resolve()
    candidate = cwd if cwd.is_absolute() else workspace_root / cwd
    return candidate.resolve()


def _record_run_history(
    history_store: HistoryStore,
    session_id: str,
    *,
    request: ShellCommandRequest,
    request_cwd: Path,
    result: ShellCommandResult | None,
    error: str | None,
    status: SessionStatus,
) -> None:
    execution_status = "executed" if result is not None and result.ok else "failed"
    history_store.record_command(
        session_id,
        action_id=None,
        source="cli.run",
        command=request.command,
        args=list(request.args),
        cwd=str(result.cwd if result is not None else request_cwd),
        mode=result.mode.value if result is not None else request.mode.value,
        policy_decision="allow",
        policy_reason="The user invoked foundation run directly.",
        risk_categories=[],
        execution_status=execution_status,
        exit_code=None if result is None else result.exit_code,
        duration_seconds=None if result is None else result.duration_seconds,
        stdout="" if result is None else result.stdout,
        stderr="" if result is None else result.stderr,
        stdout_truncated=False if result is None else result.stdout_truncated,
        stderr_truncated=False if result is None else result.stderr_truncated,
        error=error,
    )

    summary_text = (
        f"Executed shell command `{result.display_command}`."
        if result is not None and result.ok
        else error or f"Shell command `{shlex.join(request.argv)}` failed."
    )
    history_store.record_summary(
        session_id,
        assistant_message=None,
        summary_text=summary_text,
        executed_actions=1 if result is not None and result.ok else 0,
        pending_approval_actions=0,
        blocked_actions=0,
        failed_actions=0 if result is not None and result.ok else 1,
        skipped_actions=0,
    )
    history_store.finalize_session(session_id, status=status)


@app.callback()
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed Foundation CLI version and exit.",
            is_eager=True,
        ),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Path to the Foundation CLI config TOML file.",
        ),
    ] = None,
    workspace_root: Annotated[
        Path | None,
        typer.Option(
            "--workspace-root",
            help="Override the configured workspace root for this invocation.",
        ),
    ] = None,
    approval_mode: Annotated[
        ApprovalMode | None,
        typer.Option(
            "--approval-mode",
            help="Override the approval mode for this invocation.",
        ),
    ] = None,
    provider_name: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Override the configured provider name for this invocation.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Override the configured provider model for this invocation.",
        ),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(
            "--base-url",
            help="Override the configured provider base URL for this invocation.",
        ),
    ] = None,
    provider_timeout: Annotated[
        int | None,
        typer.Option(
            "--provider-timeout",
            min=1,
            help="Override the configured provider request timeout in seconds.",
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Force debug logging for this invocation.",
        ),
    ] = False,
) -> None:
    """Foundation CLI entrypoint."""
    if version:
        console.print(f"foundation {__version__}")
        raise typer.Exit()

    overrides: dict[str, Any] = {}
    if workspace_root is not None:
        _nested_override(overrides, "app.workspace_root", workspace_root)
    if approval_mode is not None:
        _nested_override(overrides, "approval.mode", approval_mode)
    if provider_name is not None:
        _nested_override(overrides, "provider.name", provider_name)
    if model is not None:
        _nested_override(overrides, "provider.model", model)
    if base_url is not None:
        _nested_override(overrides, "provider.base_url", base_url)
    if provider_timeout is not None:
        _nested_override(overrides, "provider.request_timeout_seconds", provider_timeout)
    if debug:
        _nested_override(overrides, "logging.level", LogLevel.DEBUG)

    ctx.obj = CLIContext(config_path=config_path, overrides=overrides)
    configure_logging(LogLevel.DEBUG.value if debug else LogLevel.WARNING.value)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help=(
                "Run the command from this directory. "
                "Relative paths resolve from the workspace root."
            ),
        ),
    ] = None,
    timeout_seconds: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            min=1,
            help="Override the configured timeout in seconds.",
        ),
    ] = None,
    mode: Annotated[
        ExecutionMode,
        typer.Option(
            "--mode",
            case_sensitive=False,
            help="Execution mode: buffered, stream, or pty.",
        ),
    ] = ExecutionMode.STREAM,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            help="Set NAME=VALUE in the command environment. May be repeated.",
        ),
    ] = None,
) -> None:
    """Execute a shell command inside the configured workspace."""
    settings = _load_runtime_settings(ctx)
    history_store = _build_history_store(settings)
    command_argv = list(ctx.args)
    if command_argv and command_argv[0] == "--":
        command_argv = command_argv[1:]

    if not command_argv:
        console.print("[bold red]Execution error:[/bold red] No command provided.")
        console.print("Use `foundation run -- <command> [args...]`.")
        raise typer.Exit(code=2)

    try:
        env_overlay = _parse_env_overlays(env or [])
    except ValueError as exc:
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    request_id = f"req-{uuid.uuid4().hex}"
    runtime = _build_shell_runtime(settings)
    request = ShellCommandRequest(
        command=command_argv[0],
        args=command_argv[1:],
        approval_context={"source": "cli.run", "request_id": request_id},
        cwd=cwd,
        env_overlay=env_overlay,
        timeout_seconds=timeout_seconds,
        mode=mode,
    )
    request_cwd = _resolve_cli_request_cwd(settings.workspace_root, cwd)
    session_id = history_store.start_session(
        kind=SessionKind.RUN,
        workspace_root=settings.workspace_root,
        request_cwd=request_cwd,
        approval_mode=settings.approval.mode.value,
        command_preview=shlex.join(command_argv),
    )

    logger.info(
        "command_invoked name=run mode=%s cwd=%s argv=%s",
        mode.value,
        cwd,
        command_argv,
    )

    streamed = mode is not ExecutionMode.BUFFERED
    try:
        result = runtime.execute(
            request,
            on_event=_emit_output_event if streamed else None,
        )
    except ValueError as exc:
        _record_run_history(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            result=None,
            error=str(exc),
            status=SessionStatus.FAILED,
        )
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    except ShellExecutionSpawnError as exc:
        _record_run_history(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            result=None,
            error=str(exc),
            status=SessionStatus.FAILED,
        )
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except ShellExecutionTimeout as exc:
        _record_run_history(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            result=exc.result,
            error=str(exc),
            status=SessionStatus.FAILED,
        )
        if exc.result is not None:
            _render_result_output(exc.result, streamed=streamed)
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=124) from exc
    except ShellExecutionCancelled as exc:
        _record_run_history(
            history_store,
            session_id,
            request=request,
            request_cwd=request_cwd,
            result=exc.result,
            error=str(exc),
            status=SessionStatus.FAILED,
        )
        if exc.result is not None:
            _render_result_output(exc.result, streamed=streamed)
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution cancelled:[/bold red] {exc}")
        raise typer.Exit(code=130) from exc

    _record_run_history(
        history_store,
        session_id,
        request=request,
        request_cwd=request_cwd,
        result=result,
        error=None if result.ok else result.stderr or f"Exit code {result.exit_code}",
        status=SessionStatus.COMPLETED if result.ok else SessionStatus.FAILED,
    )
    _render_result_output(result, streamed=streamed)
    _render_execution_summary(result)
    if result.exit_code is None:
        raise typer.Exit(code=1)
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


@app.command(context_settings={"allow_extra_args": True})
def chat(
    ctx: typer.Context,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help=(
                "Set the default working directory for planned shell actions. "
                "Relative paths resolve from the workspace root."
            ),
        ),
    ] = None,
    plan_only: Annotated[
        bool,
        typer.Option(
            "--plan-only",
            help="Generate and validate a structured plan without executing allowed actions.",
        ),
    ] = False,
    render_mode: Annotated[
        RenderMode,
        typer.Option(
            "--render",
            case_sensitive=False,
            help="Choose concise or verbose chat presentation.",
        ),
    ] = RenderMode.CONCISE,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the orchestration result as JSON.",
        ),
    ] = False,
    new_session: Annotated[
        bool,
        typer.Option(
            "--new",
            help="Start a fresh persistent interactive session.",
        ),
    ] = False,
    resume_session: Annotated[
        str | None,
        typer.Option(
            "--resume",
            help="Resume one persistent interactive session by id.",
        ),
    ] = None,
) -> None:
    """Run the Stage 7 interactive chat loop or the one-shot planning flow."""
    settings = _load_runtime_settings(ctx)
    request_parts = list(ctx.args)
    if request_parts and request_parts[0] == "--":
        request_parts = request_parts[1:]
    request_text = " ".join(request_parts).strip()

    logger.info(
        "command_invoked name=chat plan_only=%s render_mode=%s cwd=%s new_session=%s "
        "resume_session=%s",
        plan_only,
        render_mode.value,
        cwd,
        new_session,
        resume_session,
    )

    if new_session and resume_session is not None:
        console.print(
            "[bold red]Chat error:[/bold red] Use either `--new` or `--resume`, not both."
        )
        raise typer.Exit(code=2)

    if not request_text:
        if as_json:
            console.print(
                "[bold red]Chat error:[/bold red] `foundation chat --json` requires a request."
            )
            raise typer.Exit(code=2)
        try:
            initial_cwd = _resolve_chat_session_cwd(settings.workspace_root, cwd)
        except ValueError as exc:
            console.print(f"[bold red]Chat error:[/bold red] {exc}")
            raise typer.Exit(code=2) from exc
        resume_target = (
            ResumeTarget.explicit(resume_session)
            if resume_session is not None
            else ResumeTarget.latest()
        )
        if new_session:
            session_manager = _build_session_manager(settings)
            try:
                new_state = session_manager.create_session(
                    initial_cwd=initial_cwd,
                    approval_mode=settings.approval.mode.value,
                    model=settings.provider.model,
                )
            except ValueError as exc:
                console.print(f"[bold red]Chat error:[/bold red] {exc}")
                raise typer.Exit(code=2) from exc
            resume_target = ResumeTarget.explicit(new_state.session_id)
        _run_interactive_chat(
            settings,
            initial_cwd=initial_cwd,
            plan_only=plan_only,
            render_mode=render_mode,
            resume_target=resume_target,
        )
        return

    if new_session or resume_session is not None:
        console.print(
            "[bold red]Chat error:[/bold red] `--new` and `--resume` are only "
            "supported for interactive `foundation chat` sessions."
        )
        raise typer.Exit(code=2)

    try:
        result = _execute_chat_request(
            settings,
            message=request_text,
            cwd=cwd,
            plan_only=plan_only,
        )
    except ProviderError as exc:
        console.print(f"[bold red]Provider error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except OrchestrationPlanError as exc:
        console.print(f"[bold red]Planning error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except OrchestrationError as exc:
        console.print(f"[bold red]Orchestration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    if as_json:
        console.print_json(data=result.model_dump(mode="json"))
        return

    _render_chat_turn(result, render_mode=render_mode)


@config_app.callback()
def config_callback(ctx: typer.Context) -> None:
    """Default `foundation config` behavior."""
    if ctx.invoked_subcommand is None:
        config_show(ctx)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show the effective configuration without exposing secrets."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=config.show")
    console.print_json(data=render_settings_payload(settings))


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the effective configuration and render a concise summary."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=config.validate")
    console.print("[green]Configuration is valid.[/green]")
    console.print(f"Config path: [cyan]{settings.config_path}[/cyan]")
    console.print(f"Workspace root: [cyan]{settings.workspace_root}[/cyan]")
    console.print(f"Provider: [cyan]{settings.provider.name}[/cyan]")
    console.print(f"Model: [cyan]{settings.provider.model}[/cyan]")
    console.print(f"Base URL: [cyan]{settings.provider.effective_base_url()}[/cyan]")
    console.print(f"Request timeout: [cyan]{settings.provider.request_timeout_seconds}s[/cyan]")
    console.print(
        "Provider credential sources: "
        f"[cyan]{', '.join(settings.provider.credential_source_order()) or 'none'}[/cyan]"
    )


@config_app.command("locations")
def config_locations(ctx: typer.Context) -> None:
    """Show the key filesystem locations used by the current config."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=config.locations")

    for name, value in settings.config_locations().items():
        console.print(f"[bold]{name}[/bold]: [cyan]{value}[/cyan]")


@tool_app.command("availability")
def tools_availability(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Show which local context binaries are currently available."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=tools.availability")
    availability = _build_tool_service(settings).availability_report()
    if as_json:
        console.print_json(data=[item.model_dump(mode="json") for item in availability])
        return
    _render_availability(availability)


@tool_app.command("search")
def tools_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search query for ripgrep.")],
    scope: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Restrict the search to this path inside the workspace.",
        ),
    ] = None,
    max_results: Annotated[
        int,
        typer.Option(
            "--max-results",
            min=1,
            help="Maximum number of matches to return.",
        ),
    ] = 50,
    case_sensitive: Annotated[
        bool,
        typer.Option(
            "--case-sensitive",
            help="Use case-sensitive matching instead of ripgrep smart-case.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Search workspace content through the typed ripgrep wrapper."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=tools.search query=%s", query)
    service = _build_tool_service(settings)
    try:
        result = service.search(
            SearchRequest(
                query=query,
                scope=scope,
                max_results=max_results,
                case_sensitive=case_sensitive,
            )
        )
    except ToolExecutionError as exc:
        _render_tool_error(exc)
        raise typer.Exit(code=_tool_exit_code(exc)) from exc

    if as_json:
        console.print_json(data=result.model_dump(mode="json"))
        return
    _render_search_result(result)


@tool_app.command("files")
def tools_files(
    ctx: typer.Context,
    pattern: Annotated[str, typer.Argument(help="Pattern passed to fd.")],
    scope: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Restrict file discovery to this path inside the workspace.",
        ),
    ] = None,
    file_type: Annotated[
        FileDiscoveryType,
        typer.Option(
            "--type",
            help="Restrict discovery to files, directories, or both.",
            case_sensitive=False,
        ),
    ] = FileDiscoveryType.ANY,
    max_results: Annotated[
        int,
        typer.Option(
            "--max-results",
            min=1,
            help="Maximum number of paths to return.",
        ),
    ] = 100,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Discover workspace paths through the typed fd wrapper."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=tools.files pattern=%s", pattern)
    service = _build_tool_service(settings)
    try:
        result = service.discover_files(
            FileDiscoveryRequest(
                pattern=pattern,
                scope=scope,
                file_type=file_type,
                max_results=max_results,
            )
        )
    except ToolExecutionError as exc:
        _render_tool_error(exc)
        raise typer.Exit(code=_tool_exit_code(exc)) from exc

    if as_json:
        console.print_json(data=result.model_dump(mode="json"))
        return
    _render_file_result(result)


@tool_app.command("git")
def tools_git(
    ctx: typer.Context,
    scope: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Restrict repository context to this path inside the workspace.",
        ),
    ] = None,
    max_status_entries: Annotated[
        int,
        typer.Option(
            "--max-status",
            min=1,
            help="Maximum number of status entries to return.",
        ),
    ] = 100,
    max_recent_commits: Annotated[
        int,
        typer.Option(
            "--max-commits",
            min=1,
            help="Maximum number of recent commits to include.",
        ),
    ] = 5,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Show repository branch, status, diff, and recent commit context."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=tools.git")
    service = _build_tool_service(settings)
    try:
        result = service.git_context(
            GitContextRequest(
                scope=scope,
                max_status_entries=max_status_entries,
                max_recent_commits=max_recent_commits,
            )
        )
    except ToolExecutionError as exc:
        _render_tool_error(exc)
        raise typer.Exit(code=_tool_exit_code(exc)) from exc

    if as_json:
        console.print_json(data=result.model_dump(mode="json"))
        return
    _render_git_context(result)


def _lookup_help(
    ctx: typer.Context,
    *,
    topic: str,
    source: HelpLookupSource,
    max_characters: int,
    as_json: bool,
) -> None:
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=tools.%s topic=%s", source.value, topic)
    service = _build_tool_service(settings)
    try:
        result = service.lookup_help(
            HelpLookupRequest(
                topic=topic,
                source=source,
                max_characters=max_characters,
            )
        )
    except ToolExecutionError as exc:
        _render_tool_error(exc)
        raise typer.Exit(code=_tool_exit_code(exc)) from exc

    if as_json:
        console.print_json(data=result.model_dump(mode="json"))
        return
    _render_help_lookup(result)


@tool_app.command("man")
def tools_man(
    ctx: typer.Context,
    topic: Annotated[str, typer.Argument(help="Manual page topic.")],
    max_characters: Annotated[
        int,
        typer.Option(
            "--max-characters",
            min=1,
            help="Maximum number of characters to return.",
        ),
    ] = 8000,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Lookup a local manual page through the typed tool layer."""
    _lookup_help(
        ctx,
        topic=topic,
        source=HelpLookupSource.MAN,
        max_characters=max_characters,
        as_json=as_json,
    )


@tool_app.command("tldr")
def tools_tldr(
    ctx: typer.Context,
    topic: Annotated[str, typer.Argument(help="TLDR page topic.")],
    max_characters: Annotated[
        int,
        typer.Option(
            "--max-characters",
            min=1,
            help="Maximum number of characters to return.",
        ),
    ] = 8000,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Lookup a local TLDR page through the typed tool layer."""
    _lookup_help(
        ctx,
        topic=topic,
        source=HelpLookupSource.TLDR,
        max_characters=max_characters,
        as_json=as_json,
    )


@app.command()
def history(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of sessions to list.",
        ),
    ] = 20,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Show the detailed record for one session id.",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Inspect persisted session history."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=history session=%s limit=%s", session, limit)
    history_store = _build_history_store(settings)

    if session is not None:
        record = history_store.get_session(session)
        if record is None:
            console.print(f"[bold red]History error:[/bold red] Unknown session id {session}.")
            raise typer.Exit(code=1)
        if as_json:
            console.print_json(data=record.model_dump(mode="json"))
            return
        _render_history_detail(record)
        return

    records = history_store.list_sessions(limit=limit)
    if as_json:
        console.print_json(data=[record.model_dump(mode="json") for record in records])
        return
    _render_history_list(records)


@app.command()
def trace(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of traces to list.",
        ),
    ] = 20,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Show the trace for one session id.",
        ),
    ] = None,
    step: Annotated[
        str | None,
        typer.Option(
            "--step",
            help="Show one trace step. Requires --session.",
        ),
    ] = None,
    predecessors: Annotated[
        bool,
        typer.Option(
            "--predecessors",
            help="Include causal predecessors when --step is set.",
        ),
    ] = False,
    audit: Annotated[
        bool,
        typer.Option(
            "--audit",
            help="Render the audit report instead of the raw trace.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON instead of Rich-rendered output.",
        ),
    ] = False,
) -> None:
    """Inspect Stage 3 traces and audit reports."""
    settings = _load_runtime_settings(ctx)
    logger.info(
        "command_invoked name=trace session=%s step=%s limit=%s",
        session,
        step,
        limit,
    )
    trace_store = _build_history_store(settings)

    if step is not None and session is None:
        console.print("[bold red]Trace error:[/bold red] `--step` requires `--session`.")
        raise typer.Exit(code=2)

    if session is None:
        records = trace_store.list_traces(limit=limit)
        if as_json:
            console.print_json(data=[record.model_dump(mode="json") for record in records])
            return
        _render_trace_list(records)
        return

    query = TraceQuery(
        session_id=session,
        step_id=step,
        include_predecessors=predecessors,
        limit=limit,
    )
    if audit:
        report = trace_store.get_audit_report(query)
        if report is None:
            console.print(f"[bold red]Trace error:[/bold red] Unknown session id {session}.")
            raise typer.Exit(code=1)
        if as_json:
            console.print_json(data=report.model_dump(mode="json"))
            return
        _render_audit_report(report)
        return

    record = trace_store.get_trace(query)
    if record is None:
        console.print(f"[bold red]Trace error:[/bold red] Unknown trace or step for {session}.")
        raise typer.Exit(code=1)
    if as_json:
        console.print_json(data=record.model_dump(mode="json"))
        return
    _render_trace_detail(record)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Run environment and configuration readiness checks."""
    cli_context = ctx.obj if isinstance(ctx.obj, CLIContext) else CLIContext()
    report = run_doctor(
        config_path=cli_context.config_path,
        overrides=cli_context.overrides,
    )
    logger.info("command_invoked name=doctor exit_code=%s", report.exit_code)
    _render_doctor_report(report)
    if not report.ok:
        raise typer.Exit(code=report.exit_code)


def main() -> None:
    """Console entrypoint used by the package script."""
    app()
