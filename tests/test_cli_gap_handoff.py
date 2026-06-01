"""CLI-level tests for capability-gap handoff handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.test_cli import _write_stage_2_config

from foundation.models import GapOptionKind, LoopStopReason
from foundation.services.gap_handoff import build_gap_handoff
from foundation.settings import load_settings


def _missing_capability_handoff():
    from foundation.models import ExecutionResult, ExecutionStatus

    handoff = build_gap_handoff(
        request="store this in a database",
        stop_reason=LoopStopReason.FATAL_EXECUTION_FAILURE,
        results=[
            ExecutionResult(
                action_id="a1",
                status=ExecutionStatus.FAILED,
                summary="failed",
                error="unsupported capability: foundation.db.write",
            )
        ],
        iteration=1,
        had_cumulative_changes=False,
    )
    assert handoff is not None
    return handoff


def test_handle_gap_handoff_report_writes_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation import cli

    settings = load_settings(config_path=_write_stage_2_config(tmp_path))
    handoff = _missing_capability_handoff()
    report_option = next(o for o in handoff.options if o.kind is GapOptionKind.REPORT)
    monkeypatch.setattr(cli, "_prompt_gap_option", lambda _h: report_option)

    follow_up = cli._handle_gap_handoff(handoff, settings=settings)

    assert follow_up is None  # reporting does not resume work
    gaps_dir = settings.app.state_dir / "gaps"
    written = list(gaps_dir.glob("gap-*.json"))
    assert len(written) == 1


def test_handle_gap_handoff_alternative_returns_follow_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation import cli

    settings = load_settings(config_path=_write_stage_2_config(tmp_path))
    handoff = _missing_capability_handoff()
    alt_option = next(o for o in handoff.options if o.kind is GapOptionKind.ALTERNATIVE)
    monkeypatch.setattr(cli, "_prompt_gap_option", lambda _h: alt_option)

    follow_up = cli._handle_gap_handoff(handoff, settings=settings)

    assert follow_up == alt_option.follow_up_request
    assert "store this in a database" in follow_up


def test_handle_gap_handoff_declined_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundation import cli

    settings = load_settings(config_path=_write_stage_2_config(tmp_path))
    handoff = _missing_capability_handoff()
    monkeypatch.setattr(cli, "_prompt_gap_option", lambda _h: None)

    assert cli._handle_gap_handoff(handoff, settings=settings) is None
