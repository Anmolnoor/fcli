"""Runtime construction and orchestration helpers for Foundation CLI."""

from __future__ import annotations

import os
import shlex
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import click
import typer
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from foundation.cli_rendering import console
from foundation.live_turn import LiveTurnRenderer, get_active_renderer, live_ux_disabled
from foundation.models import (
    ApprovalRequest,
    CapabilityGapHandoff,
    CapabilityGapOption,
    GapOptionKind,
    OrchestrationResult,
    ProviderMessage,
    QuestionAction,
    SessionStatus,
    UserRequest,
)
from foundation.monitor import (
    EventLogWriter,
    LocalHttpSseTransport,
    MonitorServer,
    TransportStartError,
    UnixSocketTransport,
    compose_event_sink,
)
from foundation.services import (
    ApprovalService,
    CapabilityRegistry,
    CapabilityStore,
    HistoryStore,
    LocalToolService,
    RequestOrchestrator,
    SessionManager,
    ShellCommandRequest,
    ShellCommandResult,
    ShellRuntime,
    TraceStore,
    build_provider_adapter,
)
from foundation.services.gap_handoff import build_issue_url, write_gap_report
from foundation.settings import ApprovalMode, AppSettings

_REPL_SESSION_DB_FILENAME = "chat-sessions.sqlite3"
_PACKAGE_MANAGER_COMMANDS = frozenset({"npm", "pnpm", "yarn", "pip", "uv"})


def _build_shell_runtime(settings: AppSettings) -> ShellRuntime:
    return ShellRuntime(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=settings.shell.default_timeout_seconds,
        max_timeout_seconds=settings.shell.max_timeout_seconds,
        allow_pty=settings.shell.allow_pty,
        capture_limit_kb=settings.shell.capture_limit_kb,
        enforce_workspace_boundary=settings.shell.enforce_workspace_boundary,
        pass_through_foundation_env=settings.shell.pass_through_foundation_env,
    )


def _build_tool_service(settings: AppSettings) -> LocalToolService:
    return LocalToolService(
        workspace_root=settings.workspace_root,
        default_timeout_seconds=min(settings.shell.default_timeout_seconds, 30),
        capture_limit_kb=settings.shell.capture_limit_kb,
        pass_through_foundation_env=settings.shell.pass_through_foundation_env,
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
            f"Side effects: [yellow]{escape(', '.join(request.requested_side_effects))}[/yellow]"
        )
    package_warning = _package_manager_warning(request)
    if package_warning:
        lines.append(f"Package manager warning: [yellow]{escape(package_warning)}[/yellow]")
    if request.reason_codes:
        reason_text = ", ".join(code.value for code in request.reason_codes)
        lines.append(f"Policy reasons: [magenta]{escape(reason_text)}[/magenta]")
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
    renderer = get_active_renderer()
    if renderer is not None:
        renderer.pause()
    try:
        console.print(Panel.fit(panel_text, title="Approval Required"))
        return typer.confirm("Approve this action?", default=False)
    finally:
        if renderer is not None:
            renderer.resume()


def _package_manager_warning(request: ApprovalRequest) -> str | None:
    command_preview = request.command_preview
    if not command_preview:
        return None
    try:
        parts = shlex.split(command_preview)
    except ValueError:
        parts = command_preview.split()
    command = parts[0].split("/")[-1] if parts else ""
    if command not in _PACKAGE_MANAGER_COMMANDS:
        return None
    return (
        "package-manager commands may use the network, run package scripts, "
        "and modify package metadata or lockfiles."
    )


def _prompt_for_question(question: QuestionAction) -> str | None:
    lines = [f"[bold]{escape(question.prompt)}[/bold]"]
    if question.options:
        lines.append("")
        for index, option in enumerate(question.options, start=1):
            lines.append(f"  [cyan]{index}[/cyan]. {escape(option)}")
    renderer = get_active_renderer()
    if renderer is not None:
        renderer.pause()
    try:
        console.print(Panel.fit("\n".join(lines), title="Question"))
        try:
            raw = str(typer.prompt("Your answer"))
        except (EOFError, click.exceptions.Abort):
            return None
        answer = raw.strip()
        if not answer:
            return None
        # Allow selecting an option by its number.
        if question.options and answer.isdigit():
            choice = int(answer)
            if 1 <= choice <= len(question.options):
                return question.options[choice - 1]
        return answer
    finally:
        if renderer is not None:
            renderer.resume()


def _prompt_gap_option(handoff: CapabilityGapHandoff) -> CapabilityGapOption | None:
    """Present a capability-gap handoff and return the option the user chose."""
    lines = [f"[bold]{escape(handoff.message)}[/bold]", ""]
    for index, option in enumerate(handoff.options, start=1):
        lines.append(f"  [cyan]{index}[/cyan]. {escape(option.label)}")
    renderer = get_active_renderer()
    if renderer is not None:
        renderer.pause()
    try:
        console.print(Panel.fit("\n".join(lines), title="How would you like to proceed?"))
        try:
            raw = typer.prompt("Choose an option", default="").strip()
        except (EOFError, click.exceptions.Abort):
            return None
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(handoff.options):
                return handoff.options[choice - 1]
        return None
    finally:
        if renderer is not None:
            renderer.resume()


def _submit_gap_report(handoff: CapabilityGapHandoff, *, settings: AppSettings) -> None:
    """Persist a gap report and show the user where it can be filed/fixed."""
    path = write_gap_report(handoff.report, gaps_dir=settings.app.state_dir / "gaps")
    url = build_issue_url(handoff.report)
    console.print(
        Panel.fit(
            "Thanks — I saved a gap report so this can be fixed.\n\n"
            f"Saved to: [cyan]{escape(str(path))}[/cyan]\n"
            f"File it as an issue: [cyan]{escape(url)}[/cyan]",
            title="Reported",
        )
    )


def _handle_gap_handoff(
    handoff: CapabilityGapHandoff,
    *,
    settings: AppSettings,
) -> str | None:
    """Drive the interactive gap handoff. Return a follow-up request to resume, or None."""
    choice = _prompt_gap_option(handoff)
    if choice is None:
        return None
    if choice.kind is GapOptionKind.REPORT:
        _submit_gap_report(handoff, settings=settings)
        return None
    if choice.kind is GapOptionKind.ALTERNATIVE:
        return choice.follow_up_request
    console.print(Text("Okay — stopping here.", style="dim"))
    return None


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
        question_callback=_prompt_for_question,
        capability_registry=_build_capability_registry(settings, tool_service=tool_service),
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
    disable_live_ux: bool = False,
    disable_monitor: bool = False,
    monitor_socket: str | None = None,
    monitor_http_port: int | None = None,
) -> OrchestrationResult:
    orchestrator = _build_orchestrator(
        settings,
        approval_mode=approval_mode,
        shell_output_callback=shell_output_callback,
    )
    request = UserRequest(
        message=message,
        conversation_history=list(conversation_history or []),
        cwd=cwd,
        plan_only=plan_only,
    )
    use_live_ux = not (disable_live_ux or live_ux_disabled())
    use_monitor = settings.monitor.enabled and not disable_monitor
    use_socket = monitor_socket is not None and use_monitor
    use_http = monitor_http_port is not None and use_monitor
    if not use_live_ux and not use_monitor:
        return orchestrator.orchestrate(request)

    monitor_server: MonitorServer | None = None
    transports: list[Any] = []
    if use_socket or use_http:
        monitor_server = MonitorServer(queue_size=settings.monitor.subscriber_queue_size)
        if use_socket:
            socket_path = _resolve_monitor_socket_path(settings, override=monitor_socket)
            try:
                transports.append(UnixSocketTransport(path=socket_path, server=monitor_server))
            except TransportStartError as exc:
                console.print(f"[bold yellow]Monitor warning:[/bold yellow] {exc}")
        if use_http:
            token = _resolve_monitor_http_token(settings)
            assert monitor_http_port is not None
            try:
                transports.append(
                    LocalHttpSseTransport(
                        port=monitor_http_port,
                        token=token,
                        server=monitor_server,
                    )
                )
                console.print(
                    f"[dim]Monitor HTTP transport listening on "
                    f"127.0.0.1:{monitor_http_port}; bearer token: {token}[/dim]"
                )
            except TransportStartError as exc:
                console.print(f"[bold yellow]Monitor warning:[/bold yellow] {exc}")
    return _run_orchestrate_with_sinks(
        orchestrator,
        request,
        use_live=use_live_ux,
        writer=_build_event_log_writer(settings) if use_monitor else None,
        monitor_server=monitor_server,
        transports=transports,
    )


def _resolve_monitor_socket_path(settings: AppSettings, *, override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    if settings.monitor.socket_path is not None:
        return settings.monitor.socket_path
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    return Path(runtime_dir).expanduser() / "foundation" / f"{os.getpid()}.sock"


def _resolve_monitor_http_token(settings: AppSettings) -> str:
    configured = settings.monitor.auth_token
    if configured is not None:
        token = configured.get_secret_value()
        if token:
            return token
    import secrets

    return secrets.token_urlsafe(24)


def _build_event_log_writer(settings: AppSettings) -> EventLogWriter:
    return EventLogWriter(
        events_dir=settings.monitor.events_dir,
        max_sessions=settings.monitor.retention.max_sessions,
        max_bytes=settings.monitor.retention.max_bytes,
    )


def _run_orchestrate_with_sinks(
    orchestrator: RequestOrchestrator,
    request: UserRequest,
    *,
    use_live: bool,
    writer: EventLogWriter | None,
    monitor_server: MonitorServer | None = None,
    transports: list[Any] | None = None,
) -> OrchestrationResult:
    set_sink = getattr(orchestrator, "set_event_sink", None)

    def _attach_sink(sink: Any) -> None:
        if callable(set_sink):
            set_sink(sink)

    transports = transports or []

    def _build_sinks(extra: list[Any] | None = None) -> list[Any]:
        sinks: list[Any] = list(extra or [])
        if writer is not None:
            sinks.append(writer.write_event)
        if monitor_server is not None:
            sinks.append(monitor_server.publish)
        return sinks

    writer_ctx: Any = writer if writer is not None else _NullContext()
    server_ctx: Any = monitor_server if monitor_server is not None else _NullContext()
    transport_stack: list[Any] = []
    try:
        for transport in transports:
            transport.start()
            transport_stack.append(transport)
        with writer_ctx, server_ctx:
            if not use_live:
                _attach_sink(compose_event_sink(*_build_sinks()))
                try:
                    return orchestrator.orchestrate(request)
                finally:
                    _attach_sink(None)

            result_box: dict[str, OrchestrationResult | BaseException] = {}

            def worker() -> None:
                try:
                    result_box["result"] = orchestrator.orchestrate(request)
                except BaseException as exc:  # noqa: BLE001 - re-raised on main thread
                    result_box["error"] = exc

            with LiveTurnRenderer(console=console) as renderer:
                _attach_sink(compose_event_sink(*_build_sinks([renderer.on_event])))
                try:
                    thread = threading.Thread(target=worker, name="fcli-orchestrate", daemon=True)
                    thread.start()
                    try:
                        renderer.drain_until_finished(worker=thread)
                    except KeyboardInterrupt:
                        thread.join(timeout=5.0)
                        raise
                finally:
                    _attach_sink(None)

        error = result_box.get("error")
        if isinstance(error, BaseException):
            raise error
        result = result_box.get("result")
        if isinstance(result, OrchestrationResult):
            return result
        raise RuntimeError("Orchestration worker finished without a result.")
    finally:
        for transport in reversed(transport_stack):
            with suppress(Exception):
                transport.close()


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: Any) -> None:
        return None


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
