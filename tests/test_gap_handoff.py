"""Tests for the capability-gap handoff builder and report helpers."""

from __future__ import annotations

import json

from foundation.models import (
    CapabilityGapKind,
    ExecutionResult,
    ExecutionStatus,
    GapOptionKind,
    LoopStopReason,
    ProviderResponse,
    ProviderResponseMetadata,
)
from foundation.services.gap_handoff import (
    build_gap_handoff,
    build_issue_url,
    make_provider_phraser,
    write_gap_report,
)
from foundation.services.provider import ProviderError


class _StubProvider:
    """Minimal provider stub: returns a queued text body, or raises."""

    def __init__(self, *, body: str | None = None, error: bool = False) -> None:
        self._body = body
        self._error = error
        self.calls = 0

    def complete(self, _prompt):  # noqa: ANN001 - test stub
        self.calls += 1
        if self._error:
            raise ProviderError("boom")
        return ProviderResponse(
            content=self._body,
            structured_output=None,
            metadata=ProviderResponseMetadata(
                provider="stub",
                model="stub-model",
                latency_seconds=0.01,
            ),
        )


def _failed(error: str, action_id: str = "a1") -> ExecutionResult:
    return ExecutionResult(
        action_id=action_id,
        status=ExecutionStatus.FAILED,
        summary="action failed",
        error=error,
    )


def test_no_handoff_for_non_stuck_stop_reasons() -> None:
    for stop in (
        LoopStopReason.MAX_ITERATIONS,
        LoopStopReason.MAX_ACTIONS,
        LoopStopReason.PENDING_APPROVAL,
        LoopStopReason.AWAITING_USER_INPUT,
        LoopStopReason.ZERO_ACTION_PLAN,
    ):
        handoff = build_gap_handoff(
            request="do a thing",
            stop_reason=stop,
            results=[_failed("unsupported capability: foundation.db.write")],
            iteration=1,
            had_cumulative_changes=False,
        )
        assert handoff is None


def test_no_progress_with_changes_is_soft_completion_not_a_gap() -> None:
    handoff = build_gap_handoff(
        request="edit the file",
        stop_reason=LoopStopReason.NO_PROGRESS,
        results=[],
        iteration=2,
        had_cumulative_changes=True,
    )
    assert handoff is None


def test_missing_capability_classification_and_options() -> None:
    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("unsupported capability: foundation.db.write")],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    assert handoff.kind is CapabilityGapKind.MISSING_CAPABILITY
    assert handoff.report.detail == "foundation.db.write"
    # The message reads as a calm explanation, not a raw error dump.
    assert "unsupported capability" not in handoff.message.lower()
    assert "couldn't finish" in handoff.message.lower()
    kinds = [option.kind for option in handoff.options]
    assert GapOptionKind.REPORT in kinds
    assert GapOptionKind.STOP in kinds
    alternative = next(o for o in handoff.options if o.kind is GapOptionKind.ALTERNATIVE)
    assert alternative.follow_up_request is not None
    assert "store this in a database" in alternative.follow_up_request


def test_path_not_found_extracts_path() -> None:
    handoff = build_gap_handoff(
        request="read config",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("No such file or directory: 'config/app.toml'")],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    assert handoff.kind is CapabilityGapKind.PATH_NOT_FOUND
    assert handoff.report.detail == "config/app.toml"


def test_command_unavailable_classification() -> None:
    handoff = build_gap_handoff(
        request="run the linter",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("could not start command: ruff")],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    assert handoff.kind is CapabilityGapKind.COMMAND_UNAVAILABLE


def test_no_progress_without_error_is_stuck_with_no_alternative() -> None:
    handoff = build_gap_handoff(
        request="make it perfect",
        stop_reason=LoopStopReason.NO_PROGRESS,
        results=[],
        iteration=3,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    assert handoff.kind is CapabilityGapKind.STUCK_NO_PROGRESS
    kinds = {option.kind for option in handoff.options}
    assert kinds == {GapOptionKind.REPORT, GapOptionKind.STOP}


def test_issue_url_is_prefilled_and_labeled() -> None:
    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("unsupported capability: foundation.db.write")],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    url = build_issue_url(handoff.report)
    assert url.startswith("https://github.com/Anmolnoor/fcli/issues/new?")
    assert "labels=capability-gap" in url
    assert "title=" in url and "body=" in url


def test_phraser_overrides_message_but_keeps_options_and_report() -> None:
    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("unsupported capability: foundation.db.write")],
        iteration=1,
        had_cumulative_changes=False,
        phraser=lambda kind, request, detail, fallback: "A clearer, friendlier explanation.",
    )
    assert handoff is not None
    assert handoff.message == "A clearer, friendlier explanation."
    # Structure stays deterministic.
    assert handoff.report.detail == "foundation.db.write"
    assert {o.kind for o in handoff.options} >= {GapOptionKind.REPORT, GapOptionKind.STOP}


def test_phraser_returning_none_falls_back_to_template() -> None:
    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("unsupported capability: foundation.db.write")],
        iteration=1,
        had_cumulative_changes=False,
        phraser=lambda *_args: None,
    )
    assert handoff is not None
    assert "couldn't finish" in handoff.message.lower()


def test_make_provider_phraser_returns_sanitized_prose() -> None:
    provider = _StubProvider(body="  That data store isn't wired up in fcli yet.\n")
    phraser = make_provider_phraser(provider)
    result = phraser(CapabilityGapKind.MISSING_CAPABILITY, "save to db", "foundation.db.write", "x")
    assert result == "That data store isn't wired up in fcli yet."
    assert provider.calls == 1


def test_make_provider_phraser_rejects_json_like_output() -> None:
    provider = _StubProvider(body='{"assistant_message": "Done.", "actions": []}')
    phraser = make_provider_phraser(provider)
    assert phraser(CapabilityGapKind.MISSING_CAPABILITY, "r", "d", "fallback") is None


def test_make_provider_phraser_handles_provider_error() -> None:
    phraser = make_provider_phraser(_StubProvider(error=True))
    assert phraser(CapabilityGapKind.STUCK_NO_PROGRESS, "r", "", "fallback") is None


def test_make_provider_phraser_truncates_overlong_output() -> None:
    provider = _StubProvider(body="word " * 200)
    phraser = make_provider_phraser(provider)
    result = phraser(CapabilityGapKind.UNKNOWN, "r", "", "fallback")
    assert result is not None
    assert len(result) <= 401  # _MAX_PHRASED_CHARS + ellipsis
    assert result.endswith("…")


def test_write_gap_report_round_trips(tmp_path) -> None:
    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[_failed("unsupported capability: foundation.db.write")],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    path = write_gap_report(handoff.report, gaps_dir=tmp_path / "gaps")
    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded["request"] == "store this in a database"
    assert loaded["gap_kind"] == "missing_capability"
    assert loaded["detail"] == "foundation.db.write"
    assert loaded["stop_reason"] == "fatal_execution_failure"
    # Deterministic id: a second write of the same report reuses the same file.
    again = write_gap_report(handoff.report, gaps_dir=tmp_path / "gaps")
    assert again == path
