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
    OllamaChatAdapter,
    OpenAIResponsesAdapter,
    ProviderError,
    ProviderErrorCode,
    _try_extract_json,
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


def test_ollama_adapter_parses_structured_output_without_api_key() -> None:
    transport = FakeTransport(
        [
            {
                "model": "gpt-oss:120b-cloud",
                "message": {
                    "role": "assistant",
                    "content": '{"assistant_message":"hello","actions":[]}',
                },
                "prompt_eval_count": 21,
                "eval_count": 9,
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="gpt-oss:120b-cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "hello", "actions": []}
    assert response.metadata.provider == "ollama"
    assert response.metadata.model == "gpt-oss:120b-cloud"
    assert response.metadata.usage is not None
    assert response.metadata.usage.input_tokens == 21
    assert response.metadata.usage.output_tokens == 9
    assert response.metadata.usage.total_tokens == 30
    assert transport.calls[0]["url"] == "http://localhost:11434/api/chat"
    assert transport.calls[0]["headers"] == {}
    assert transport.calls[0]["payload"]["format"] == {"type": "object"}


def test_ollama_adapter_sends_authorization_when_api_key_is_configured() -> None:
    transport = FakeTransport(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "plain text response",
                }
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="glm-4.7:cloud",
        api_key="ollama-secret",
        base_url="https://ollama.com/api",
        transport=transport,
    )
    prompt = ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Say hi.",
            )
        ]
    )

    response = adapter.complete(prompt)

    assert response.content == "plain text response"
    assert transport.calls[0]["url"] == "https://ollama.com/api/chat"
    assert transport.calls[0]["headers"] == {
        "Authorization": "Bearer ollama-secret",
    }


def test_ollama_adapter_uses_thinking_as_fallback_only_for_freeform_calls() -> None:
    """For free-form (non-JSON) calls, thinking is an acceptable content fallback."""
    transport = FakeTransport(
        [
            {
                "model": "qwen3.5:397b-cloud",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "just reasoning out loud",
                },
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="qwen3.5:397b-cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )
    freeform_prompt = ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Say hi.",
            )
        ]
    )

    response = adapter.complete(freeform_prompt)

    assert response.content == "just reasoning out loud"


def test_ollama_adapter_rejects_thinking_only_response_for_json_calls() -> None:
    """JSON_OBJECT calls must NOT fall back to thinking — it's never valid JSON output."""
    transport = FakeTransport(
        [
            {
                "model": "deepseek-v3.2:cloud",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "# 两数之和\n\n这是一个经典的LeetCode题目...",
                },
                "prompt_eval_count": 120,
                "eval_count": 300,
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="deepseek-v3.2:cloud",
        base_url="https://ollama.com/api",
        transport=transport,
    )

    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(_structured_prompt())

    assert excinfo.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert "thinking tokens" in str(excinfo.value)


def test_ollama_adapter_sends_think_true_for_qwen3_structured_output() -> None:
    """Qwen 3.x needs think=true with format to reason about the JSON schema."""
    transport = FakeTransport(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"assistant_message":"ok","actions":[]}',
                },
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="qwen3.5:397b-cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    adapter.complete(_structured_prompt())

    assert transport.calls[0]["payload"]["think"] is True


def test_ollama_adapter_omits_think_for_non_qwen3_structured_output() -> None:
    """Non-Qwen3 models (e.g. deepseek) regress into free-form thinking when
    think=true is forced, so the adapter must not send it."""
    transport = FakeTransport(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"assistant_message":"ok","actions":[]}',
                },
            }
        ]
    )
    adapter = OllamaChatAdapter(
        model="deepseek-v3.2:cloud",
        base_url="https://ollama.com/api",
        transport=transport,
    )

    adapter.complete(_structured_prompt())

    assert "think" not in transport.calls[0]["payload"]


# ---- _try_extract_json tests ----


def test_try_extract_json_clean_passthrough() -> None:
    raw = '{"assistant_message":"hi","actions":[]}'
    assert _try_extract_json(raw) == raw


def test_try_extract_json_strips_whitespace() -> None:
    raw = '  \n {"key": "value"}  \n '
    assert _try_extract_json(raw) == '{"key": "value"}'


def test_try_extract_json_code_fence_json() -> None:
    raw = 'Here is the plan:\n```json\n{"assistant_message":"hi","actions":[]}\n```\n'
    assert _try_extract_json(raw) == '{"assistant_message":"hi","actions":[]}'


def test_try_extract_json_code_fence_bare() -> None:
    raw = 'Sure:\n```\n{"key": "val"}\n```'
    assert _try_extract_json(raw) == '{"key": "val"}'


def test_try_extract_json_preamble_text() -> None:
    raw = 'I will help you. {"assistant_message":"ok","actions":[]}'
    assert _try_extract_json(raw) == '{"assistant_message":"ok","actions":[]}'


def test_try_extract_json_nested_braces() -> None:
    raw = 'text before {"outer": {"inner": 1}} text after'
    assert _try_extract_json(raw) == '{"outer": {"inner": 1}}'


def test_try_extract_json_no_json_returns_stripped() -> None:
    raw = "I cannot help with that request."
    assert _try_extract_json(raw) == raw.strip()


# ---- Structured output extraction integration tests ----


def test_ollama_adapter_handles_code_fenced_json() -> None:
    """Models that wrap JSON in code fences should still parse successfully."""
    fenced = '```json\n{"assistant_message":"hello","actions":[]}\n```'
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": fenced}}]
    )
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "hello", "actions": []}


def test_ollama_adapter_handles_preamble_json() -> None:
    """Models that include preamble text before JSON should still parse."""
    preamble = 'Sure, here is the plan:\n{"assistant_message":"ok","actions":[]}'
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": preamble}}]
    )
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "ok", "actions": []}


def test_openai_adapter_handles_code_fenced_json() -> None:
    """OpenAI adapter should also handle code-fenced JSON from non-compliant models."""
    fenced = '```json\n{"assistant_message":"hello","actions":[]}\n```'
    transport = FakeTransport(
        [
            {
                "id": "resp_456",
                "output": [{"content": [{"type": "output_text", "text": fenced}]}],
            }
        ]
    )
    adapter = OpenAIResponsesAdapter(
        model="test-model",
        api_key="test-key",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "hello", "actions": []}


def test_ollama_adapter_invalid_json_error_includes_raw() -> None:
    """Error message should include a preview of what the model actually returned."""
    garbage = "This is not JSON at all, just plain text."
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": garbage}}]
    )
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    with pytest.raises(ProviderError, match="Raw \\(first 300 chars\\)") as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert exc_info.value.response_text == garbage
