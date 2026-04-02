from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from foundation.models import (
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
)
from foundation.services.provider import (
    OpenAIResponsesAdapter,
    ProviderError,
    ProviderErrorCode,
)


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _structured_prompt() -> ProviderPrompt:
    return ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Plan this request.",
            )
        ],
        response_format=ProviderResponseFormat.JSON_OBJECT,
        schema_name="assistant_plan",
        output_schema={"type": "object"},
    )


def test_openai_adapter_parses_structured_output_and_usage() -> None:
    transport = FakeTransport(
        [
            {
                "id": "resp_123",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"assistant_message":"hello","actions":[]}',
                            }
                        ]
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                },
            }
        ]
    )
    adapter = OpenAIResponsesAdapter(
        model="gpt-5-mini",
        api_key="test-key",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "hello", "actions": []}
    assert response.metadata.provider == "openai"
    assert response.metadata.model == "gpt-5-mini"
    assert response.metadata.attempts == 1
    assert response.metadata.usage is not None
    assert response.metadata.usage.total_tokens == 18
    assert transport.calls[0]["payload"]["text"]["format"] == {
        "type": "json_schema",
        "name": "assistant_plan",
        "schema": {"type": "object"},
        "strict": True,
    }


def test_openai_adapter_retries_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foundation.services.provider.time.sleep", lambda *_args: None)
    transport = FakeTransport(
        [
            ProviderError(
                "slow down",
                code=ProviderErrorCode.RATE_LIMIT,
                retryable=True,
                status_code=429,
            ),
            {
                "id": "resp_retry",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"assistant_message":"ok","actions":[]}',
                            }
                        ]
                    }
                ],
            },
        ]
    )
    adapter = OpenAIResponsesAdapter(
        model="gpt-5-mini",
        api_key="test-key",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "ok", "actions": []}
    assert response.metadata.attempts == 2
    assert len(transport.calls) == 2


def test_openai_adapter_does_not_retry_non_retryable_failures() -> None:
    transport = FakeTransport(
        [
            ProviderError(
                "bad api key",
                code=ProviderErrorCode.AUTHENTICATION,
                retryable=False,
                status_code=401,
            )
        ]
    )
    adapter = OpenAIResponsesAdapter(
        model="gpt-5-mini",
        api_key="test-key",
        transport=transport,
    )

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.AUTHENTICATION
    assert len(transport.calls) == 1
