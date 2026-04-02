"""Typer application entrypoint for Foundation CLI."""

from __future__ import annotations

import logging
import shlex
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
    ApprovalRequest,
    ExecutionArtifactType,
    ExecutionResult,
    HistorySessionDetail,
    HistorySessionSummary,
    OrchestrationResult,
    SessionKind,
    SessionStatus,
    UserRequest,
)
from foundation.services import (
    ApprovalService,
    ExecutionMode,
    FileDiscoveryRequest,
    FileDiscoveryResult,
    FileDiscoveryType,
    GitContextRequest,
    GitContextResult,
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


@dataclass(slots=True)
class CLIContext:
    """Global CLI options that participate in settings resolution."""

    config_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


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

    configure_logging(settings.logging.level.value)
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
    return HistoryStore(
        database_path=settings.history.database_path,
        retention_days=settings.history.retention_days,
        max_entries=settings.history.max_entries,
    )


def _prompt_for_approval(request: ApprovalRequest) -> bool:
    risk_text = ", ".join(request.risk_categories) if request.risk_categories else "unknown"
    lines = [
        f"Action: [cyan]{escape(request.action_id)}[/cyan]",
        f"Summary: {escape(request.summary)}",
        f"Reason: {escape(request.reason)}",
        f"Risk: [yellow]{escape(risk_text)}[/yellow]",
    ]
    if request.command_preview:
        lines.append(f"Command: [cyan]{escape(request.command_preview)}[/cyan]")
    if request.cwd:
        lines.append(f"Cwd: [cyan]{escape(request.cwd)}[/cyan]")
    if request.paths:
        lines.append(f"Paths: [cyan]{escape(', '.join(request.paths))}[/cyan]")
    console.print(Panel.fit("\n".join(lines), title="Approval Required"))
    return typer.confirm("Approve this action?", default=False)


def _build_orchestrator(settings: AppSettings) -> RequestOrchestrator:
    return RequestOrchestrator(
        workspace_root=settings.workspace_root,
        approval_mode=settings.approval.mode,
        provider=build_provider_adapter(settings),
        shell_runtime=_build_shell_runtime(settings),
        tool_service=_build_tool_service(settings),
        approval_service=ApprovalService(
            mode=settings.approval.mode,
            prompt_callback=_prompt_for_approval,
        ),
        history_store=_build_history_store(settings),
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
            "Captured output truncated: "
            f"[yellow]{escape(', '.join(truncated_streams))}[/yellow]"
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
            f"[{style}]{item.status.value.upper():<9}[/{style}] "
            f"{item.name}: {required}{resolved}"
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
            f"Scope: [cyan]{result.scope}[/cyan]\n"
            f"Branch: [cyan]{result.branch}[/cyan]",
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


def _render_action_plan(result: OrchestrationResult) -> None:
    if not result.plan.actions:
        console.print(Text("No actions planned.", style="dim"))
        return

    decisions = {decision.action_id: decision for decision in result.policy_decisions}
    table = Table(title="Planned Actions")
    table.add_column("Id", style="cyan")
    table.add_column("Kind")
    table.add_column("Summary")
    table.add_column("Policy")
    for action in result.plan.actions:
        decision = decisions.get(action.id)
        policy_text = "-" if decision is None else decision.decision.value
        table.add_row(action.id, action.kind.value, action.summary, policy_text)
    console.print(table)


def _render_chat_execution_result(result: ExecutionResult) -> None:
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
        _render_result_output(shell_result, streamed=False)
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
        table.add_column("Status")
        table.add_column("Mode")
        table.add_column("Reason")
        for approval in session.approvals:
            table.add_row(
                approval.action_id or "-",
                approval.status.value,
                approval.mode,
                approval.reason,
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
        else error
        or f"Shell command `{shlex.join(request.argv)}` failed."
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

    runtime = _build_shell_runtime(settings)
    request = ShellCommandRequest(
        command=command_argv[0],
        args=command_argv[1:],
        cwd=cwd,
        env_overlay=env_overlay,
        timeout_seconds=timeout_seconds,
        mode=mode,
        approval_context={"source": "cli.run"},
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
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the orchestration result as JSON.",
        ),
    ] = False,
) -> None:
    """Run the Stage 5 one-shot planning and execution flow."""
    settings = _load_runtime_settings(ctx)
    request_parts = list(ctx.args)
    if request_parts and request_parts[0] == "--":
        request_parts = request_parts[1:]
    request_text = " ".join(request_parts).strip()

    logger.info("command_invoked name=chat plan_only=%s cwd=%s", plan_only, cwd)

    if not request_text:
        _render_placeholder(
            "chat",
            (
                "Interactive chat will be implemented in stage 7.\n"
                "Stage 5 supports one-shot requests, for example:\n"
                "`foundation chat summarize the current git status`"
            ),
            settings,
        )
        return

    orchestrator = _build_orchestrator(settings)
    try:
        result = orchestrator.orchestrate(
            UserRequest(
                message=request_text,
                cwd=cwd,
                plan_only=plan_only,
            )
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

    _render_assistant_message(result)
    _render_action_plan(result)
    for execution_result in result.execution_results:
        _render_chat_execution_result(execution_result)
    _render_orchestration_summary(result)


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
