"""Typer application entrypoint for Foundation CLI."""

from __future__ import annotations

import enum
import logging
import os
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from foundation import __version__
from foundation.cli_interactive import (
    _resolve_chat_session_cwd,
    _run_interactive_chat,
)
from foundation.cli_rendering import (
    _emit_output_event,
    _render_audit_report,
    _render_availability,
    _render_chat_turn,
    _render_doctor_report,
    _render_execution_summary,
    _render_file_result,
    _render_git_context,
    _render_help_lookup,
    _render_history_detail,
    _render_history_list,
    _render_result_output,
    _render_search_result,
    _render_tool_error,
    _render_trace_detail,
    _render_trace_list,
    _tool_exit_code,
    console,
)
from foundation.cli_runtime import (
    _build_history_store,
    _build_session_manager,
    _build_shell_runtime,
    _build_tool_service,
    _execute_chat_request,
    _record_run_history,
    _resolve_cli_request_cwd,
)
from foundation.doctor import run_doctor
from foundation.logging import configure_logging
from foundation.models import (
    RenderMode,
    ResumeTarget,
    SessionKind,
    SessionStatus,
    TerminalLogRouting,
    TraceQuery,
)
from foundation.services import (
    ExecutionMode,
    FileDiscoveryRequest,
    FileDiscoveryType,
    GitContextRequest,
    HelpLookupRequest,
    HelpLookupSource,
    OrchestrationError,
    OrchestrationPlanError,
    ProviderError,
    SearchRequest,
    ShellCommandRequest,
    ShellExecutionCancelled,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ToolExecutionError,
)
from foundation.settings import (
    ApprovalMode,
    AppSettings,
    LogLevel,
    SettingsLoadError,
    load_settings,
    render_settings_payload,
)


class AgentInvocationMode(enum.Enum):
    """How the agent was invoked from the CLI."""

    INTERACTIVE = "interactive"
    ONE_SHOT = "one_shot"


class CLIRequestRoute(enum.Enum):
    """Resolved routing decision for a top-level CLI invocation."""

    ADMIN_SUBCOMMAND = "admin_subcommand"
    AGENT_INTERACTIVE = "agent_interactive"
    AGENT_ONE_SHOT = "agent_one_shot"


# ---------------------------------------------------------------------------
# Admin subcommand names that take precedence over agent routing.
# Any bare token matching one of these is dispatched as an admin command,
# not as one-shot agent request text.
# ---------------------------------------------------------------------------
_ADMIN_SUBCOMMANDS: frozenset[str] = frozenset(
    {"run", "tools", "history", "trace", "config", "doctor", "chat"}
)


class FoundationGroup(TyperGroup):
    """CLI group that routes bare and one-shot invocations to the agent.

    Routing rules:
    1. If the first positional token matches a registered subcommand, dispatch
       to that subcommand (admin precedence).
    2. If there are positional tokens but the first one is *not* a registered
       subcommand, treat them all as one-shot agent request text and forward to
       the ``chat`` command.
    3. If there are no positional tokens, forward to the ``chat`` command with
       no arguments (interactive shell).
    """

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Route unknown first tokens to the chat command."""
        if not args:
            # Should not normally be called with no args, but guard anyway.
            chat_cmd = self.get_command(ctx, "chat")
            return "chat", chat_cmd, []

        cmd_name = click.utils.make_str(args[0])
        cmd = self.get_command(ctx, cmd_name)

        if cmd is not None:
            return cmd_name, cmd, args[1:]

        # Not a recognised subcommand — forward all tokens to `chat` so they
        # become the one-shot request text.
        chat_cmd = self.get_command(ctx, "chat")
        return "chat", chat_cmd, args

    def invoke(self, ctx: click.Context) -> Any:
        """Route bare invocation (no args) to the agent interactive shell."""
        if not ctx._protected_args and not ctx.args:
            # Bare `foundation` — synthesise a chat sub-invocation.
            ctx._protected_args = ["chat"]
        return super().invoke(ctx)


app = typer.Typer(
    name="foundation",
    help=(
        "Foundation CLI — a local-first, shell-native coding agent.\n\n"
        "Run `foundation` to start the interactive agent shell.\n"
        "Run `foundation <request>` for a one-shot agent turn.\n"
        "Use admin subcommands (run, tools, config, doctor, …) for "
        "operational tasks."
    ),
    cls=FoundationGroup,
    no_args_is_help=False,
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

logger = logging.getLogger("foundation.cli")


@dataclass(slots=True)
class CLIContext:
    """Global CLI options that participate in settings resolution."""

    config_path: Path | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    disable_live_ux: bool = False
    disable_monitor: bool = False
    monitor_socket: str | None = None
    monitor_http_port: int | None = None


def _nested_override(root: dict[str, Any], dotted_path: str, value: Any) -> None:
    target = root
    path_parts = dotted_path.split(".")
    for part in path_parts[:-1]:
        target = target.setdefault(part, {})
    target[path_parts[-1]] = value


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
    no_live: Annotated[
        bool,
        typer.Option(
            "--no-live",
            help="Disable the in-turn live status widget.",
        ),
    ] = False,
    no_monitor: Annotated[
        bool,
        typer.Option(
            "--no-monitor",
            help="Disable the persistent NDJSON event log for this invocation.",
        ),
    ] = False,
    events_dir: Annotated[
        Path | None,
        typer.Option(
            "--events-dir",
            help="Override the events directory for the persistent NDJSON event log.",
        ),
    ] = None,
    monitor_socket: Annotated[
        str | None,
        typer.Option(
            "--monitor-socket",
            help=(
                "Enable the live Unix-socket monitor transport. Pass a path "
                "to override the default ${TMPDIR}/foundation/<pid>.sock."
            ),
        ),
    ] = None,
    monitor_http: Annotated[
        int | None,
        typer.Option(
            "--monitor-http",
            min=1,
            max=65535,
            help=(
                "Enable the live HTTP/SSE monitor transport on the given "
                "localhost port. A bearer token is printed at startup."
            ),
        ),
    ] = None,
) -> None:
    """Foundation CLI — local-first, shell-native coding agent.

    Run ``foundation`` to open the interactive agent shell.
    Run ``foundation <request>`` for a one-shot agent turn.
    """
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

    if events_dir is not None:
        _nested_override(overrides, "monitor.events_dir", events_dir)
    env_monitor = os.environ.get("FOUNDATION_MONITOR", "").strip().lower()
    monitor_env_off = env_monitor in {"0", "false", "no", "off"}
    env_socket = os.environ.get("FOUNDATION_MONITOR_SOCKET", "").strip()
    env_http = os.environ.get("FOUNDATION_MONITOR_HTTP", "").strip()
    resolved_socket = monitor_socket
    if resolved_socket is None and env_socket:
        # Empty/"1" → default-path; non-empty → explicit override.
        resolved_socket = "" if env_socket in {"1", "true", "yes", "on"} else env_socket
    resolved_http_port = monitor_http
    if resolved_http_port is None and env_http:
        try:
            resolved_http_port = int(env_http)
        except ValueError:
            pass
    ctx.obj = CLIContext(
        config_path=config_path,
        overrides=overrides,
        disable_live_ux=no_live,
        disable_monitor=no_monitor or monitor_env_off,
        monitor_socket=resolved_socket,
        monitor_http_port=resolved_http_port,
    )
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
    headless: Annotated[
        bool,
        typer.Option(
            "--headless",
            help="Run one contract task noninteractively (worker mode).",
        ),
    ] = False,
    task_file: Annotated[
        Path | None,
        typer.Option(
            "--task-file",
            help="Path to the contract task.json envelope (requires --headless).",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Path to write the contract result.json (requires --headless).",
        ),
    ] = None,
) -> None:
    """Execute a shell command inside the configured workspace."""
    settings = _load_runtime_settings(ctx)
    if headless or task_file is not None or out is not None:
        if not headless or task_file is None or out is None:
            console.print(
                "[bold red]Execution error:[/bold red] headless mode requires "
                "--headless, --task-file, and --out together."
            )
            raise typer.Exit(code=2)
        from foundation.headless import run_headless_task

        raise typer.Exit(code=run_headless_task(task_file, out, settings=settings))
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


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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
    """Run the interactive agent shell or a one-shot agent turn.

    This is the shared implementation behind both ``foundation`` and
    ``foundation chat``.  When invoked without request text the interactive
    shell opens; when request text is provided a single agent turn executes.
    """
    settings = _load_runtime_settings(ctx)
    request_parts = list(ctx.args)
    if request_parts and request_parts[0] == "--":
        request_parts = request_parts[1:]
    request_text = " ".join(request_parts).strip()

    # Reject explicit empty-string input like `foundation ""`.
    if not request_text and request_parts:
        console.print(
            "[bold red]Chat error:[/bold red] Empty request text. "
            "Use `foundation` for the interactive shell or "
            "`foundation <request>` for a one-shot turn."
        )
        raise typer.Exit(code=2)

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
        cli_ctx = ctx.obj if isinstance(ctx.obj, CLIContext) else None
        _run_interactive_chat(
            settings,
            initial_cwd=initial_cwd,
            plan_only=plan_only,
            render_mode=render_mode,
            resume_target=resume_target,
            disable_live_ux=bool(cli_ctx and cli_ctx.disable_live_ux),
            disable_monitor=bool(cli_ctx and cli_ctx.disable_monitor),
            monitor_socket=cli_ctx.monitor_socket if cli_ctx else None,
            monitor_http_port=cli_ctx.monitor_http_port if cli_ctx else None,
        )
        return

    if new_session or resume_session is not None:
        console.print(
            "[bold red]Chat error:[/bold red] `--new` and `--resume` are only "
            "supported for interactive `foundation chat` sessions."
        )
        raise typer.Exit(code=2)

    cli_ctx = ctx.obj if isinstance(ctx.obj, CLIContext) else None
    try:
        result = _execute_chat_request(
            settings,
            message=request_text,
            cwd=cwd,
            plan_only=plan_only,
            disable_live_ux=bool(cli_ctx and cli_ctx.disable_live_ux),
            disable_monitor=bool(cli_ctx and cli_ctx.disable_monitor),
            monitor_socket=cli_ctx.monitor_socket if cli_ctx else None,
            monitor_http_port=cli_ctx.monitor_http_port if cli_ctx else None,
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
