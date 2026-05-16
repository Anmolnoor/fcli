"""Concise chat-turn notice builders.

These helpers translate orchestration results into the small set of typed
`ChatNotice` items shown above the assistant reply in concise rendering mode.
They are pure functions: no I/O, no logging, no global state. Keeping them
in their own module lets `cli.py` stay focused on the Typer surface and lets
tests exercise the formatters in isolation.
"""

from __future__ import annotations

from foundation.models import (
    ActionKind,
    ChatNotice,
    ExecutionArtifactType,
    ExecutionStatus,
    LoopStopReason,
    OrchestrationResult,
    PresentationNoticeLevel,
    VerificationOutcome,
)

_CODE_CHANGING_ARTIFACT_TYPES = frozenset(
    {
        ExecutionArtifactType.FILE_WRITE,
        ExecutionArtifactType.FILE_EDIT,
        ExecutionArtifactType.FILE_APPLY_DIFF,
    }
)

_CHANGED_FILES_DISPLAY_CAP = 6
_COMMANDS_RUN_DISPLAY_CAP = 6


def iteration_changed_files_notice(
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


def iteration_commands_notice(
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


def verification_outcome_notice(
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


def approval_required_notice(
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


__all__ = [
    "iteration_changed_files_notice",
    "iteration_commands_notice",
    "verification_outcome_notice",
    "approval_required_notice",
]
