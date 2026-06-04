"""Rich rendering helpers for Foundation CLI."""

from __future__ import annotations

import shlex

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from foundation.doctor import DoctorReport, DoctorStatus
from foundation.models import (
    ActionKind,
    AuditDetailRef,
    AuditReport,
    ChatNotice,
    ChatSurfacePolicy,
    ChatTurnPresentation,
    ExecutionArtifactType,
    ExecutionResult,
    ExecutionStatus,
    HistorySessionDetail,
    HistorySessionSummary,
    InteractiveDetailCommand,
    LoopStopReason,
    OrchestrationResult,
    PlanningStep,
    PresentationNoticeLevel,
    RenderMode,
    TraceRecord,
    TraceSummary,
    VerificationOutcome,
)
from foundation.services import (
    ExecutionMode,
    FileDiscoveryResult,
    GitContextResult,
    HelpLookupResult,
    OutputStream,
    SearchResult,
    ShellCommandResult,
    ShellOutputEvent,
    ToolAvailabilityStatus,
    ToolBinaryStatus,
    ToolErrorCode,
    ToolExecutionError,
)
from foundation.services.gap_handoff import build_issue_url
from foundation.settings import AppSettings

console = Console()
stderr_console = Console(stderr=True)

_REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS = 1200


_INTERACTIVE_CONCISE_OUTPUT_PADDING = "  "


def _format_interactive_concise_text(text: str) -> str:
    return "\n".join(
        f"{_INTERACTIVE_CONCISE_OUTPUT_PADDING}{line}" if line else line
        for line in text.splitlines()
    )


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


def _preview_transcript_text(value: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        return ""
    if len(trimmed) <= _REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS:
        return trimmed
    return trimmed[:_REPL_TRANSCRIPT_OUTPUT_PREVIEW_CHARACTERS].rstrip() + "\n...[truncated]"


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
    if result.artifact_type in (ExecutionArtifactType.MAN, ExecutionArtifactType.TLDR):
        help_result = HelpLookupResult.model_validate(result.artifact)
        content_preview = _preview_transcript_text(help_result.content)
        if not content_preview:
            return None
        return ChatNotice(
            level=PresentationNoticeLevel.DIM,
            text=f"{help_result.source.value}: {help_result.topic}\n{content_preview}",
        )
    return None


_CODE_CHANGING_ARTIFACT_TYPES = frozenset(
    {
        ExecutionArtifactType.FILE_WRITE,
        ExecutionArtifactType.FILE_EDIT,
        ExecutionArtifactType.FILE_APPLY_DIFF,
    }
)


_CHANGED_FILES_DISPLAY_CAP = 6


_COMMANDS_RUN_DISPLAY_CAP = 6


def _iteration_changed_files_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Dedup and summarize file paths changed across all iterations."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for item in result.execution_results:
        if item.artifact_type in _CODE_CHANGING_ARTIFACT_TYPES and item.artifact is not None:
            path = item.artifact.get("path")
            if isinstance(path, str) and path and path not in seen_set:
                seen.append(path)
                seen_set.add(path)
    if not seen:
        return None
    shown = seen[:_CHANGED_FILES_DISPLAY_CAP]
    suffix = ""
    if len(seen) > _CHANGED_FILES_DISPLAY_CAP:
        suffix = f", +{len(seen) - _CHANGED_FILES_DISPLAY_CAP} more"
    label = "Changed file" if len(seen) == 1 else "Changed files"
    return ChatNotice(
        level=PresentationNoticeLevel.INFO,
        text=f"{label}: {', '.join(shown)}{suffix}",
    )


def _iteration_commands_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Collect shell commands run across iterations, dedup consecutive duplicates."""
    commands: list[str] = []
    for iteration in result.iterations:
        for action, exec_result in zip(
            iteration.plan.actions,
            iteration.execution_results,
            strict=False,
        ):
            if action.kind is not ActionKind.SHELL or action.shell is None:
                continue
            if exec_result.status is not ExecutionStatus.EXECUTED:
                continue
            parts = [action.shell.command, *action.shell.args]
            display = " ".join(parts).strip()
            if not display:
                continue
            if len(display) > 80:
                display = display[:77] + "..."
            if commands and commands[-1] == display:
                continue
            commands.append(display)
    if not commands:
        return None
    shown = commands[:_COMMANDS_RUN_DISPLAY_CAP]
    suffix = ""
    if len(commands) > _COMMANDS_RUN_DISPLAY_CAP:
        suffix = f"\n  +{len(commands) - _COMMANDS_RUN_DISPLAY_CAP} more"
    label = "Command" if len(commands) == 1 else "Commands"
    formatted = "\n  ".join(f"$ {cmd}" for cmd in shown)
    return ChatNotice(
        level=PresentationNoticeLevel.DIM,
        text=f"{label} run:\n  {formatted}{suffix}",
    )


def _verification_outcome_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Render the orchestrator's verification notice as a user-facing notice."""
    notice = result.verification_notice
    if notice is None:
        return None
    outcome = notice.outcome
    if outcome is VerificationOutcome.PASSED:
        cmds = ", ".join(notice.verification_commands_run[:3])
        detail = f" ({cmds})" if cmds else ""
        return ChatNotice(
            level=PresentationNoticeLevel.INFO,
            text=f"Verification: passed{detail}",
        )
    if outcome is VerificationOutcome.FAILED:
        cmds = ", ".join(notice.verification_commands_run[:3])
        detail = f" ({cmds})" if cmds else ""
        return ChatNotice(
            level=PresentationNoticeLevel.WARNING,
            text=f"Verification: failed{detail}",
        )
    if outcome is VerificationOutcome.UNAVAILABLE:
        cmds = ", ".join(notice.verification_commands_run[:3])
        detail = f" ({cmds})" if cmds else ""
        return ChatNotice(
            level=PresentationNoticeLevel.WARNING,
            text=f"Verification: unavailable{detail}",
        )
    # NOT_ATTEMPTED
    return ChatNotice(
        level=PresentationNoticeLevel.WARNING,
        text="Verification: code changed but no verification command ran",
    )


def _approval_required_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Surface pending-approval stops as a warning-level notice."""
    if result.stop_reason is not LoopStopReason.PENDING_APPROVAL:
        return None
    pending = result.summary.pending_approval_actions
    plural = "s" if pending != 1 else ""
    return ChatNotice(
        level=PresentationNoticeLevel.WARNING,
        text=(
            f"Approval required for {pending} action{plural}. "
            "Run `foundation approve` or re-issue with approval mode set."
        ),
    )


def _denied_recovery_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Offer recovery choices when the user denied an approval prompt."""
    if result.stop_reason is not LoopStopReason.BLOCKED:
        return None
    if not any(
        item.status is ExecutionStatus.BLOCKED
        and item.error is not None
        and "denied by the user" in item.error.lower()
        for item in result.execution_results
    ):
        return None
    return ChatNotice(
        level=PresentationNoticeLevel.WARNING,
        text=("Next: continue with read-only analysis, retry with approval, or stop."),
    )


def _awaiting_input_notice(
    result: OrchestrationResult,
) -> ChatNotice | None:
    """Surface an unanswered question (non-interactive / dismissed) as a notice."""
    if result.stop_reason is not LoopStopReason.AWAITING_USER_INPUT:
        return None
    prompts = [
        str(item.artifact.get("question", "")).strip()
        for item in result.execution_results
        if item.artifact_type is ExecutionArtifactType.QUESTION
        and item.artifact is not None
        and item.status is ExecutionStatus.AWAITING_INPUT
    ]
    question_text = next((prompt for prompt in prompts if prompt), "a question")
    return ChatNotice(
        level=PresentationNoticeLevel.WARNING,
        text=(
            f'Waiting on your input: "{question_text}" '
            "Re-run with your answer in the request to continue."
        ),
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

    for iteration_notice in (
        _iteration_changed_files_notice(result),
        _denied_recovery_notice(result),
        _iteration_commands_notice(result),
        _verification_outcome_notice(result),
        _approval_required_notice(result),
        _awaiting_input_notice(result),
    ):
        if iteration_notice is not None and iteration_notice.text not in seen_messages:
            notices.append(iteration_notice)
            seen_messages.add(iteration_notice.text)

    # One-shot/non-TTY runs can't prompt for a gap-handoff choice, so surface the
    # options and the report link inline. Interactive runs handle this via a prompt.
    if result.gap_handoff is not None and not interactive:
        handoff = result.gap_handoff
        option_lines = "\n".join(
            f"  {index}. {option.label}" for index, option in enumerate(handoff.options, start=1)
        )
        gap_text = (
            f"What you can do:\n{option_lines}\n"
            f"To report it so it can be fixed, file: {build_issue_url(handoff.report)}"
        )
        if gap_text not in seen_messages:
            notices.append(ChatNotice(level=PresentationNoticeLevel.WARNING, text=gap_text))
            seen_messages.add(gap_text)

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
    primary_text = presentation.primary_text
    if interactive:
        primary_text = _format_interactive_concise_text(primary_text)
    console.print(primary_text)
    style_map = {
        PresentationNoticeLevel.INFO: "cyan",
        PresentationNoticeLevel.WARNING: "yellow",
        PresentationNoticeLevel.ERROR: "bold red",
        PresentationNoticeLevel.DIM: "dim",
    }
    for notice in presentation.notices:
        notice_text = notice.text
        if interactive:
            notice_text = _format_interactive_concise_text(notice_text)
        console.print(Text(notice_text, style=style_map[notice.level]))


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
            if isinstance(step, PlanningStep):
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
