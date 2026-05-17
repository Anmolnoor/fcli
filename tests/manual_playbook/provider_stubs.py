"""Deterministic provider stubs for the playbook.

Lifted from ``tests/test_integration_e2e.py`` so playbook scenarios can pin
exact planning responses without touching a live LLM. Each scenario owns its
own response sequence via ``Scenario.stub_responses``.
"""

from __future__ import annotations

import json
from typing import Any

from foundation.models import (
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseMetadata,
)


def provider_response(payload: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(payload),
        structured_output=payload,
        metadata=ProviderResponseMetadata(
            provider="stub",
            model="stub-model",
            latency_seconds=0.01,
        ),
    )


def zero_action_response(message: str) -> ProviderResponse:
    return provider_response({"assistant_message": message, "actions": []})


class StubProvider:
    """Return queued responses; default to a zero-action completion when drained."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self._responses = list(responses)

    def complete(self, _prompt: ProviderPrompt) -> ProviderResponse:
        if not self._responses:
            return zero_action_response("Done.")
        return self._responses.pop(0)
