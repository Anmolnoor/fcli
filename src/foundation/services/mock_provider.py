"""Deterministic scripted provider — the acceptance gate never depends on a live LLM.

The mock is a first-class provider implementation (decision Q10): the golden
smoke and CI run against it with zero secrets, and a full headless run is
byte-deterministic because every "model" response comes from a scenario file.

Scenario file format (JSON):

```json
{
  "responses": [
    {"plan": {"assistant_message": "...", "actions": [...]}},
    {"review": {"decision": "accept", "reason": "..."}},
    {"directive": "crash", "exit_code": 13},
    {"directive": "hang"}
  ]
}
```

- ``plan`` entries are consumed, in order, by planning calls; once the script is
  exhausted every further planning call gets a zero-action completion.
- ``review`` entries answer plan-review preflights; without one queued, reviews
  are auto-accepted (and consume nothing).
- ``directive`` entries simulate worker failure modes for the golden smoke:
  ``crash`` exits the process immediately (no terminal event, no result);
  ``hang`` SIGSTOPs the process so heartbeats stop and the supervisor's
  death path (Q3) has something real to kill.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from foundation.models import (
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
)

MOCK_PROVIDER_NAME = "mock"


class MockScenarioError(Exception):
    """The scenario file is missing or malformed (doctor-style message)."""


def _response(payload: dict[str, Any], *, scenario: str) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(payload, sort_keys=True),
        structured_output=payload,
        metadata=ProviderResponseMetadata(
            provider=MOCK_PROVIDER_NAME,
            model=scenario,
            latency_seconds=0.0,
        ),
    )


class MockProvider:
    """Replay a scripted scenario file; deterministic by construction."""

    def __init__(self, scenario_file: Path) -> None:
        if not scenario_file.is_file():
            raise MockScenarioError(
                f"mock provider scenario file not found: {scenario_file}. "
                "Remediation: set provider.scenario_file to an existing scenario "
                "JSON (see foundation.services.mock_provider for the format)."
            )
        try:
            document = json.loads(scenario_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MockScenarioError(
                f"mock provider scenario file {scenario_file} is not valid JSON: {exc}."
            ) from exc
        responses = document.get("responses")
        if not isinstance(responses, list):
            raise MockScenarioError(
                f"mock provider scenario file {scenario_file} must contain a "
                "top-level 'responses' array."
            )
        self._entries: list[dict[str, Any]] = list(responses)
        self._scenario_name = scenario_file.stem

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        if prompt.schema_name == "assistant_plan_review":
            if self._entries and "review" in self._entries[0]:
                entry = self._entries.pop(0)
                return _response(dict(entry["review"]), scenario=self._scenario_name)
            return _response(
                {"decision": "accept", "reason": "Mock preflight accepted the plan."},
                scenario=self._scenario_name,
            )

        if not self._entries:
            return _response(
                {"assistant_message": "Done.", "actions": []},
                scenario=self._scenario_name,
            )

        entry = self._entries.pop(0)
        if "plan" in entry:
            return _response(dict(entry["plan"]), scenario=self._scenario_name)
        if "directive" in entry:
            return self._run_directive(entry)
        raise MockScenarioError(
            f"mock scenario entry must contain 'plan', 'review', or 'directive': {entry!r}"
        )

    def _run_directive(self, entry: dict[str, Any]) -> ProviderResponse:
        directive = str(entry["directive"])
        if directive == "crash":
            # Simulate a worker that dies mid-run: no terminal event, no result.
            os._exit(int(entry.get("exit_code", 13)))
        if directive == "hang":
            # Freeze the whole process (all threads, heartbeats included) so the
            # supervisor's heartbeat-loss detection has something real to kill.
            os.kill(os.getpid(), signal.SIGSTOP)
            time.sleep(3600)  # unreachable unless resumed; the supervisor kills us
            return _response(
                {"assistant_message": "Resumed after hang.", "actions": []},
                scenario=self._scenario_name,
            )
        raise MockScenarioError(f"unknown mock directive {directive!r}; supported: crash, hang.")
