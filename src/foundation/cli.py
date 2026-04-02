"""Typer application entrypoint for Foundation CLI."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from foundation import __version__
from foundation.doctor import DoctorReport, DoctorStatus, run_doctor
from foundation.logging import configure_logging
from foundation.services import (
    ExecutionMode,
    OutputStream,
    ShellCommandRequest,
    ShellCommandResult,
    ShellExecutionCancelled,
    ShellExecutionSpawnError,
    ShellExecutionTimeout,
    ShellOutputEvent,
    ShellRuntime,
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
app.add_typer(config_app, name="config")

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
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    except ShellExecutionSpawnError as exc:
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except ShellExecutionTimeout as exc:
        if exc.result is not None:
            _render_result_output(exc.result, streamed=streamed)
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution error:[/bold red] {exc}")
        raise typer.Exit(code=124) from exc
    except ShellExecutionCancelled as exc:
        if exc.result is not None:
            _render_result_output(exc.result, streamed=streamed)
            _render_execution_summary(exc.result)
        console.print(f"[bold red]Execution cancelled:[/bold red] {exc}")
        raise typer.Exit(code=130) from exc

    _render_result_output(result, streamed=streamed)
    _render_execution_summary(result)
    if result.exit_code is None:
        raise typer.Exit(code=1)
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


@app.command()
def chat(ctx: typer.Context) -> None:
    """Placeholder command for the interactive session."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=chat")
    _render_placeholder("chat", "Interactive chat will be implemented in stage 7.", settings)


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


@app.command()
def history(ctx: typer.Context) -> None:
    """Placeholder command for session history."""
    settings = _load_runtime_settings(ctx)
    logger.info("command_invoked name=history")
    _render_placeholder(
        "history",
        "Persistent history will be implemented in stage 6.",
        settings,
    )


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
