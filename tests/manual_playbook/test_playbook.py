"""Run the manual playbook scenarios under deterministic stub providers.

This is the CI-safe layer of the playbook. The live-mode entrypoint lives in
``tests.manual_playbook.live_runner`` and reuses the same scenarios and graders
against a real provider, gated behind ``FOUNDATION_PLAYBOOK_LIVE=1``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.manual_playbook.harness import run_playbook_scenario
from tests.manual_playbook.provider_stubs import StubProvider
from tests.manual_playbook.scenarios import SCENARIOS
from tests.manual_playbook.scenarios._base import Scenario


@pytest.mark.parametrize(
    "scenario",
    SCENARIOS,
    ids=lambda s: s.name,
)
def test_playbook_scenario_stub_mode(
    scenario: Scenario,
    playbook_workspace: Path,
    orchestrator_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubProvider(scenario.stub_responses(playbook_workspace))
    orchestrator = orchestrator_factory(
        provider=provider,
        approval_mode=scenario.approval_mode,
    )
    run = run_playbook_scenario(
        scenario,
        workspace=playbook_workspace,
        orchestrator=orchestrator,
        monkeypatch=monkeypatch,
    )

    if run.hard_failures:
        rendered = "\n".join(outcome.render() for outcome in run.outcomes)
        pytest.fail(f"Scenario {scenario.name} failed:\n{rendered}")
