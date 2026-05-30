"""Build graceful capability-gap handoffs from stuck-loop failures.

When the bounded replan loop stops because the agent is *structurally* stuck —
the user asked for something that needs a capability fcli does not have, a path
that does not exist, or the planner spun without making progress — the
orchestrator reframes the failure as a :class:`CapabilityGapHandoff` instead of
surfacing a raw error. The user sees a plain-language explanation and a few
options (retry a constrained version, report the gap, or stop). The underlying
failure is still recorded in the trace and event log by the caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from foundation.models.orchestration import (
    CapabilityGapHandoff,
    CapabilityGapKind,
    CapabilityGapOption,
    CapabilityGapReport,
    ExecutionResult,
    ExecutionStatus,
    GapOptionKind,
    LoopStopReason,
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
)
from foundation.services.provider import ProviderAdapter, ProviderError

logger = logging.getLogger("foundation.services.gap_handoff")

# A phraser turns (kind, request, detail, fallback) into a natural-language gap
# message, or returns None to keep the deterministic fallback.
GapMessagePhraser = Callable[[CapabilityGapKind, str, str, str], "str | None"]

# Stop reasons that mean "the agent is stuck", not "work still in progress".
GAP_STOP_REASONS = frozenset(
    {LoopStopReason.FATAL_EXECUTION_FAILURE, LoopStopReason.NO_PROGRESS}
)

ISSUE_BASE_URL = "https://github.com/Anmolnoor/fcli/issues/new"

_MISSING_CAPABILITY_PATTERNS = ("unsupported capability", "invalid_capability")
_PATH_NOT_FOUND_PATTERNS = ("no such file or directory",)
_COMMAND_UNAVAILABLE_PATTERNS = (
    "failed to start",
    "could not start command",
    "command not found",
)

# Pull a capability id (foundation.x.y) or a quoted/space-delimited path out of
# an error string so the report and message can name the missing piece.
_CAPABILITY_ID_RE = re.compile(r"(foundation\.[a-z0-9_.]+)", re.IGNORECASE)
_PATH_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def build_gap_handoff(
    *,
    request: str,
    stop_reason: LoopStopReason,
    results: list[ExecutionResult],
    iteration: int,
    had_cumulative_changes: bool,
    phraser: GapMessagePhraser | None = None,
) -> CapabilityGapHandoff | None:
    """Return a handoff for a stuck-loop stop, or None when no reframe applies.

    Returns None for non-stuck stop reasons and for the soft NO_PROGRESS case
    where the workspace already reflects the user's intent (the planner merely
    re-issued finished work).

    When ``phraser`` is supplied it may rephrase the user-facing message in
    natural language; the deterministic template is used as the fallback if it
    declines (returns None) or fails. The options and report stay deterministic.
    """
    if stop_reason not in GAP_STOP_REASONS:
        return None
    if stop_reason is LoopStopReason.NO_PROGRESS and had_cumulative_changes:
        return None

    failing = _failing_result(results)
    last_error = failing.error if failing is not None else None
    kind = _classify(stop_reason, last_error)
    detail = _detail_from_error(kind, last_error)

    report = CapabilityGapReport(
        request=request,
        gap_kind=kind,
        stop_reason=stop_reason,
        detail=detail,
        last_error=last_error,
        iteration=iteration,
    )
    message, options = _message_and_options(kind, request=request, detail=detail)
    if phraser is not None:
        phrased = phraser(kind, request, detail, message)
        if phrased:
            message = phrased
    return CapabilityGapHandoff(
        kind=kind,
        message=message,
        options=options,
        report=report,
    )


def _failing_result(results: list[ExecutionResult]) -> ExecutionResult | None:
    """Most recent result that carries an error, preferring failed/blocked ones."""
    for result in reversed(results):
        if result.status in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED} and result.error:
            return result
    for result in reversed(results):
        if result.error:
            return result
    return None


def _classify(stop_reason: LoopStopReason, error: str | None) -> CapabilityGapKind:
    if stop_reason is LoopStopReason.NO_PROGRESS and not error:
        return CapabilityGapKind.STUCK_NO_PROGRESS
    text = (error or "").lower()
    if any(p in text for p in _MISSING_CAPABILITY_PATTERNS):
        return CapabilityGapKind.MISSING_CAPABILITY
    if any(p in text for p in _PATH_NOT_FOUND_PATTERNS):
        return CapabilityGapKind.PATH_NOT_FOUND
    if any(p in text for p in _COMMAND_UNAVAILABLE_PATTERNS):
        return CapabilityGapKind.COMMAND_UNAVAILABLE
    if stop_reason is LoopStopReason.NO_PROGRESS:
        return CapabilityGapKind.STUCK_NO_PROGRESS
    return CapabilityGapKind.UNKNOWN


def _detail_from_error(kind: CapabilityGapKind, error: str | None) -> str:
    if not error:
        return ""
    if kind is CapabilityGapKind.MISSING_CAPABILITY:
        match = _CAPABILITY_ID_RE.search(error)
        if match:
            return match.group(1)
    if kind is CapabilityGapKind.PATH_NOT_FOUND:
        match = _PATH_RE.search(error)
        if match:
            return match.group(1)
    return ""


def _constrained_retry(request: str, note: str) -> str:
    """Reword the original request with a note so the loop gets a second shot."""
    return f"{request}\n\n(Note from a previous attempt: {note})"


def _message_and_options(
    kind: CapabilityGapKind,
    *,
    request: str,
    detail: str,
) -> tuple[str, list[CapabilityGapOption]]:
    suffix = f": {detail}" if detail else ""
    report_option = CapabilityGapOption(
        label="Report this so it can be fixed",
        kind=GapOptionKind.REPORT,
    )
    stop_option = CapabilityGapOption(label="Stop here", kind=GapOptionKind.STOP)

    if kind is CapabilityGapKind.MISSING_CAPABILITY:
        message = (
            f"I couldn't finish that — it needs an ability fcli doesn't have yet{suffix}. "
            "This is a gap in the tool itself, not anything you did wrong."
        )
        retry = CapabilityGapOption(
            label="Have fcli retry using only the tools it does have",
            kind=GapOptionKind.ALTERNATIVE,
            follow_up_request=_constrained_retry(
                request,
                "a needed capability is unavailable. Accomplish this using only the "
                "available file, git, and shell capabilities, or clearly explain what "
                "specifically cannot be done.",
            ),
        )
        return message, [retry, report_option, stop_option]

    if kind is CapabilityGapKind.PATH_NOT_FOUND:
        message = (
            f"I couldn't find what this needs on disk{suffix}. "
            "The path may be wrong, or the file may not exist yet."
        )
        retry = CapabilityGapOption(
            label="Have fcli look for the right file and retry",
            kind=GapOptionKind.ALTERNATIVE,
            follow_up_request=_constrained_retry(
                request,
                f"a path was not found{suffix}. First locate the correct file, then proceed.",
            ),
        )
        return message, [retry, report_option, stop_option]

    if kind is CapabilityGapKind.COMMAND_UNAVAILABLE:
        message = (
            f"A command this needs isn't available in your environment{suffix}."
        )
        retry = CapabilityGapOption(
            label="Have fcli retry without that command",
            kind=GapOptionKind.ALTERNATIVE,
            follow_up_request=_constrained_retry(
                request,
                f"a required command was unavailable{suffix}. Use a different approach "
                "that does not rely on it.",
            ),
        )
        return message, [retry, report_option, stop_option]

    if kind is CapabilityGapKind.STUCK_NO_PROGRESS:
        message = (
            "I kept trying but couldn't make further progress on this. "
            "I may be missing a detail only you can provide — feel free to rephrase or "
            "add specifics, or report it if this looks like a gap in fcli."
        )
        return message, [report_option, stop_option]

    message = (
        f"I ran into something I couldn't get past{suffix}. "
        "It may be a gap in fcli rather than anything on your end."
    )
    retry = CapabilityGapOption(
        label="Have fcli try a different approach",
        kind=GapOptionKind.ALTERNATIVE,
        follow_up_request=_constrained_retry(
            request,
            "the previous approach failed. Try a different one using only the available "
            "file, git, and shell capabilities.",
        ),
    )
    return message, [retry, report_option, stop_option]


# --------------------------------------------------------------------------
# Model-backed message phrasing (hybrid: deterministic structure, natural text)
# --------------------------------------------------------------------------

_MAX_PHRASED_CHARS = 400

_KIND_DESCRIPTIONS = {
    CapabilityGapKind.MISSING_CAPABILITY: (
        "the task needs an ability (capability) that fcli does not have yet"
    ),
    CapabilityGapKind.PATH_NOT_FOUND: (
        "a file or path the task depends on does not exist on disk"
    ),
    CapabilityGapKind.COMMAND_UNAVAILABLE: (
        "a command-line tool the task needs is not available in this environment"
    ),
    CapabilityGapKind.STUCK_NO_PROGRESS: (
        "fcli kept trying but could not make further progress"
    ),
    CapabilityGapKind.UNKNOWN: "fcli hit a problem it could not get past",
}


def make_provider_phraser(provider: ProviderAdapter) -> GapMessagePhraser:
    """Build a phraser that asks the provider to phrase the gap message.

    Any provider failure (or output that doesn't look like plain prose) returns
    None so the caller falls back to the deterministic template — phrasing must
    never crash the handoff it is decorating.
    """

    def phrase(
        kind: CapabilityGapKind,
        request: str,
        detail: str,
        fallback: str,
    ) -> str | None:
        try:
            response = provider.complete(
                _build_phrasing_prompt(kind, request, detail, fallback)
            )
        except ProviderError:
            return None
        except Exception:  # pragma: no cover - defensive on the recovery path
            logger.debug("gap-message phrasing failed", exc_info=True)
            return None
        return _sanitize_phrased_message(response.content)

    return phrase


def _build_phrasing_prompt(
    kind: CapabilityGapKind,
    request: str,
    detail: str,
    fallback: str,
) -> ProviderPrompt:
    developer = (
        "You are fcli, a local coding-agent CLI. A task could not be completed "
        "and you must explain why to the user in one short, calm paragraph "
        "(1-2 sentences, under 300 characters). Be specific and plain-spoken. "
        "Frame a missing ability as a gap in the tool, not the user's fault. Do "
        "NOT output JSON, code, stack traces, raw error text, or a list of next "
        "steps (the options are shown separately). Output only the explanation."
    )
    user = (
        f"Why the task is blocked: {_KIND_DESCRIPTIONS[kind]}.\n"
        f"Specific detail involved: {detail or '(none)'}\n"
        f"What the user originally asked: {request}\n\n"
        f"A baseline phrasing to improve on (keep its meaning, make it natural): "
        f"{fallback}"
    )
    return ProviderPrompt(
        messages=[
            ProviderMessage(role=ProviderMessageRole.DEVELOPER, content=developer),
            ProviderMessage(role=ProviderMessageRole.USER, content=user),
        ],
        response_format=ProviderResponseFormat.TEXT,
    )


def _sanitize_phrased_message(content: str | None) -> str | None:
    text = (content or "").strip()
    if not text:
        return None
    # Reject anything that looks like a JSON plan or fenced code rather than prose.
    if text[0] in "{[" or text.startswith("```"):
        return None
    if '"actions"' in text or '"assistant_message"' in text:
        return None
    text = " ".join(text.split())
    if len(text) > _MAX_PHRASED_CHARS:
        text = text[:_MAX_PHRASED_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def build_issue_body(report: CapabilityGapReport) -> str:
    return (
        "**What I asked fcli to do:**\n"
        f"{report.request}\n\n"
        f"**What's missing:** {report.gap_kind.value}"
        f"{f' — {report.detail}' if report.detail else ''}\n\n"
        f"**Stop reason:** {report.stop_reason.value}\n"
        f"**Iteration:** {report.iteration}\n"
        f"**Last error:** {report.last_error or '(none recorded)'}\n\n"
        "_Filed automatically by fcli's capability-gap handoff._"
    )


def build_issue_url(report: CapabilityGapReport) -> str:
    title = f"[capability gap] {report.gap_kind.value}"
    if report.detail:
        title += f": {report.detail}"
    body = build_issue_body(report)
    return (
        f"{ISSUE_BASE_URL}?title={quote(title)}"
        f"&body={quote(body)}&labels=capability-gap"
    )


def gap_report_id(report: CapabilityGapReport) -> str:
    digest = hashlib.sha1(
        f"{report.request}|{report.gap_kind.value}|{report.detail}|{report.iteration}".encode()
    ).hexdigest()
    return digest[:12]


def write_gap_report(report: CapabilityGapReport, *, gaps_dir: Path) -> Path:
    """Persist a gap report as JSON under ``gaps_dir`` and return its path."""
    gaps_dir.mkdir(parents=True, exist_ok=True)
    path = gaps_dir / f"gap-{gap_report_id(report)}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return path
