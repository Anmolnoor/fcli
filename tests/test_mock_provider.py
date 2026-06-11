"""Mock provider: deterministic scripted responses, no network, no secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundation.models import ProviderPrompt
from foundation.services.mock_provider import MockProvider, MockScenarioError
from foundation.services.provider import ProviderError, build_provider_adapter
from foundation.settings import AppSettings, ProviderSection


def _scenario(tmp_path: Path, responses: list[dict[str, object]]) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"responses": responses}))
    return path


def _plan_prompt() -> ProviderPrompt:
    return ProviderPrompt(
        messages=[{"role": "user", "content": "do the thing"}],
        schema_name="assistant_plan",
    )


def _review_prompt() -> ProviderPrompt:
    return ProviderPrompt(
        messages=[{"role": "user", "content": "review the plan"}],
        schema_name="assistant_plan_review",
    )


def test_scripted_plans_replay_in_order_and_deterministically(tmp_path: Path) -> None:
    plan_one = {"assistant_message": "First.", "actions": []}
    plan_two = {"assistant_message": "Second.", "actions": []}
    scenario = _scenario(tmp_path, [{"plan": plan_one}, {"plan": plan_two}])

    first_run = [MockProvider(scenario).complete(_plan_prompt()).content for _ in range(1)]
    provider = MockProvider(scenario)
    assert provider.complete(_plan_prompt()).structured_output == plan_one
    assert provider.complete(_plan_prompt()).structured_output == plan_two
    # Exhausted scripts settle into zero-action completion.
    assert provider.complete(_plan_prompt()).structured_output == {
        "assistant_message": "Done.",
        "actions": [],
    }
    # Determinism: a fresh provider on the same scenario yields identical bytes.
    assert MockProvider(scenario).complete(_plan_prompt()).content == first_run[0]


def test_review_prompts_auto_accept_without_consuming(tmp_path: Path) -> None:
    plan = {"assistant_message": "Only plan.", "actions": []}
    provider = MockProvider(_scenario(tmp_path, [{"plan": plan}]))
    review = provider.complete(_review_prompt())
    assert review.structured_output == {
        "decision": "accept",
        "reason": "Mock preflight accepted the plan.",
    }
    assert provider.complete(_plan_prompt()).structured_output == plan


def test_scripted_review_entry_is_consumed(tmp_path: Path) -> None:
    provider = MockProvider(
        _scenario(tmp_path, [{"review": {"decision": "revise", "reason": "nope"}}])
    )
    review = provider.complete(_review_prompt())
    assert review.structured_output == {"decision": "revise", "reason": "nope"}


def test_missing_scenario_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(MockScenarioError, match="Remediation"):
        MockProvider(tmp_path / "absent.json")


def test_factory_builds_mock_and_requires_scenario_file(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path, [])
    settings = AppSettings(
        provider=ProviderSection(name="mock", scenario_file=scenario),
    )
    adapter = build_provider_adapter(settings)
    assert isinstance(adapter, MockProvider)

    with pytest.raises(ProviderError, match="scenario_file"):
        build_provider_adapter(AppSettings(provider=ProviderSection(name="mock")))
