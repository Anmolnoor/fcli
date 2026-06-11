from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from foundation.models import (
    ProviderMessage,
    ProviderMessageRole,
    ProviderPrompt,
    ProviderResponseFormat,
)
from foundation.services.provider import (
    CodexExecAdapter,
    OllamaChatAdapter,
    OpenAIResponsesAdapter,
    ProviderError,
    ProviderErrorCode,
    _try_extract_json,
    build_provider_adapter,
)
from foundation.settings import AppSettings


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


class FakeCodexRunner:
    def __init__(self, final_message: str) -> None:
        self.final_message = final_message
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(args[args.index("--output-last-message") + 1])
        schema = None
        if "--output-schema" in args:
            schema_path = Path(args[args.index("--output-schema") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.calls.append(
            {
                "args": list(args),
                "cwd": cwd,
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
                "schema": schema,
            }
        )
        output_path.write_text(self.final_message, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"type":"thread.started"}\n{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}\n',
            stderr="",
        )


class ScriptedCodexRunner:
    """Codex runner fake for failure paths: raises or returns a scripted result."""

    def __init__(
        self,
        *,
        exception: Exception | None = None,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        final_message: str | None = None,
    ) -> None:
        self._exception = exception
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._final_message = final_message

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        if self._exception is not None:
            raise self._exception
        if self._final_message is not None:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(self._final_message, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=args,
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


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


def test_codex_adapter_runs_codex_exec_with_chatgpt_managed_auth(
    tmp_path: Path,
) -> None:
    runner = FakeCodexRunner('{"assistant_message":"hello","actions":[]}')
    adapter = CodexExecAdapter(
        model="gpt-5.5",
        workspace_root=tmp_path,
        timeout_seconds=90,
        runner=runner,
    )
    prompt = ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Plan this request.",
            )
        ],
        response_format=ProviderResponseFormat.JSON_OBJECT,
        schema_name="assistant_plan",
        output_schema={
            "type": "object",
            "properties": {
                "assistant_message": {"type": "string"},
                "actions": {"type": "array"},
                "mode": {"$ref": "#/$defs/Mode", "default": "auto"},
                "arguments": {"properties": {"path": {"type": "string"}}},
            },
            "$defs": {"Mode": {"type": "string", "enum": ["auto", "manual"]}},
        },
    )

    response = adapter.complete(prompt)

    assert response.structured_output == {"assistant_message": "hello", "actions": []}
    assert response.metadata.provider == "codex"
    assert response.metadata.model == "gpt-5.5"
    assert response.metadata.usage is not None
    assert response.metadata.usage.total_tokens == 15
    assert runner.calls[0]["cwd"] == tmp_path
    assert runner.calls[0]["timeout_seconds"] == 90
    assert "<user>\nPlan this request.\n</user>" in runner.calls[0]["input_text"]
    assert runner.calls[0]["schema"] == {
        "type": "object",
        "properties": {
            "assistant_message": {"type": "string"},
            "actions": {"type": "array"},
            "mode": {"$ref": "#/$defs/Mode"},
            "arguments": {
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
                "required": ["path"],
            },
        },
        "$defs": {"Mode": {"type": "string", "enum": ["auto", "manual"]}},
        "additionalProperties": False,
        "required": ["assistant_message", "actions", "mode", "arguments"],
    }
    assert runner.calls[0]["args"][-1] == "-"
    assert runner.calls[0]["args"][:8] == [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.5",
    ]


def test_build_provider_adapter_selects_codex_without_openai_api_key(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        app={"workspace_root": tmp_path},
        provider={"name": "codex", "model": "gpt-5.5"},
    )

    adapter = build_provider_adapter(settings)

    assert isinstance(adapter, CodexExecAdapter)


def test_codex_adapter_omits_output_schema_for_open_ended_json_schema(
    tmp_path: Path,
) -> None:
    runner = FakeCodexRunner('{"arguments":{"path":"README.md"}}')
    adapter = CodexExecAdapter(
        model="gpt-5.5",
        workspace_root=tmp_path,
        runner=runner,
    )
    prompt = ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Plan this request.",
            )
        ],
        response_format=ProviderResponseFormat.JSON_OBJECT,
        schema_name="assistant_plan",
        output_schema={
            "type": "object",
            "properties": {
                "arguments": {
                    "type": "object",
                    "additionalProperties": True,
                }
            },
        },
    )

    response = adapter.complete(prompt)

    assert response.structured_output == {"arguments": {"path": "README.md"}}
    assert "--output-schema" not in runner.calls[0]["args"]
    assert runner.calls[0]["schema"] is None
    assert '"additionalProperties": true' in runner.calls[0]["input_text"]


def _codex_adapter(
    tmp_path: Path,
    runner: ScriptedCodexRunner,
    *,
    timeout_seconds: int = 60,
) -> CodexExecAdapter:
    return CodexExecAdapter(
        model="gpt-5.5",
        workspace_root=tmp_path,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


def test_codex_adapter_missing_binary_maps_to_bad_request(tmp_path: Path) -> None:
    runner = ScriptedCodexRunner(exception=FileNotFoundError("codex"))
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.BAD_REQUEST
    assert exc_info.value.retryable is False
    assert "Codex CLI was not found on PATH" in str(exc_info.value)


def test_codex_adapter_timeout_maps_to_retryable_network_error(tmp_path: Path) -> None:
    runner = ScriptedCodexRunner(
        exception=subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=5)
    )
    adapter = _codex_adapter(tmp_path, runner, timeout_seconds=5)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.NETWORK
    assert exc_info.value.retryable is True
    assert "timed out after 5s" in str(exc_info.value)


def test_codex_adapter_launch_oserror_maps_to_retryable_network_error(
    tmp_path: Path,
) -> None:
    runner = ScriptedCodexRunner(exception=OSError("argument list too long"))
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.NETWORK
    assert exc_info.value.retryable is True
    assert "failed to start" in str(exc_info.value)
    assert "argument list too long" in str(exc_info.value)


def test_codex_adapter_auth_failure_stderr_maps_to_authentication(
    tmp_path: Path,
) -> None:
    stderr = "Error: not logged in. Run `codex login` and sign in with ChatGPT."
    runner = ScriptedCodexRunner(returncode=1, stderr=stderr)
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.AUTHENTICATION
    assert exc_info.value.retryable is False
    assert str(exc_info.value) == stderr


def test_codex_adapter_usage_limit_stderr_maps_to_retryable_rate_limit(
    tmp_path: Path,
) -> None:
    stderr = "You've hit your usage limit. Try again later."
    runner = ScriptedCodexRunner(returncode=1, stderr=stderr)
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.RATE_LIMIT
    assert exc_info.value.retryable is True
    assert str(exc_info.value) == stderr


@pytest.mark.parametrize(
    ("returncode", "expected_retryable"),
    [(1, False), (2, True)],
)
def test_codex_adapter_nonzero_exit_preserves_stderr_in_server_error(
    tmp_path: Path,
    returncode: int,
    expected_retryable: bool,
) -> None:
    stderr = "stream disconnected before completion: unexpected status"
    runner = ScriptedCodexRunner(returncode=returncode, stderr=stderr)
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.SERVER_ERROR
    assert exc_info.value.retryable is expected_retryable
    assert str(exc_info.value) == stderr


def test_codex_adapter_nonzero_exit_prefers_error_event_from_stdout(
    tmp_path: Path,
) -> None:
    runner = ScriptedCodexRunner(
        returncode=1,
        stdout='{"type":"error","message":"model stream closed unexpectedly"}\n',
        stderr="exit status 1",
    )
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.SERVER_ERROR
    assert str(exc_info.value) == "model stream closed unexpectedly"
    assert "exit status 1" in (exc_info.value.response_text or "")


def test_codex_adapter_malformed_json_output_maps_to_invalid_response(
    tmp_path: Path,
) -> None:
    runner = ScriptedCodexRunner(final_message="Sorry, I cannot produce JSON for that request.")
    adapter = _codex_adapter(tmp_path, runner)

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert "invalid JSON" in str(exc_info.value)
    assert "Sorry, I cannot produce JSON" in str(exc_info.value)


def test_codex_adapter_empty_output_maps_to_invalid_response(tmp_path: Path) -> None:
    runner = ScriptedCodexRunner(returncode=0, stdout="")
    adapter = _codex_adapter(tmp_path, runner)
    prompt = ProviderPrompt(
        messages=[
            ProviderMessage(
                role=ProviderMessageRole.USER,
                content="Say hello.",
            )
        ],
    )

    with pytest.raises(ProviderError) as exc_info:
        adapter.complete(prompt)

    assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert "no final assistant message" in str(exc_info.value)


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


def test_ollama_adapter_retries_empty_chat_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient empty completion (no content, no thinking) is retried."""
    monkeypatch.setattr("foundation.services.provider.time.sleep", lambda *_args: None)
    transport = FakeTransport(
        [
            {
                "model": "qwen3.5:397b-cloud",
                "message": {"role": "assistant", "content": "", "thinking": None},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 15120,
                "eval_count": 1,
            },
            {
                "model": "qwen3.5:397b-cloud",
                "message": {
                    "role": "assistant",
                    "content": '{"assistant_message":"ok","actions":[]}',
                },
                "prompt_eval_count": 15120,
                "eval_count": 9,
            },
        ]
    )
    adapter = OllamaChatAdapter(
        model="qwen3.5:397b-cloud",
        base_url="https://ollama.com/api",
        transport=transport,
    )

    response = adapter.complete(_structured_prompt())

    assert response.structured_output == {"assistant_message": "ok", "actions": []}
    assert response.metadata.attempts == 2
    assert len(transport.calls) == 2


def test_ollama_adapter_gives_up_on_persistent_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every attempt is empty, it raises after exhausting retries."""
    monkeypatch.setattr("foundation.services.provider.time.sleep", lambda *_args: None)
    empty = {
        "model": "qwen3.5:397b-cloud",
        "message": {"role": "assistant", "content": "", "thinking": None},
        "done": True,
        "done_reason": "stop",
        "eval_count": 1,
    }
    transport = FakeTransport([dict(empty), dict(empty), dict(empty)])
    adapter = OllamaChatAdapter(
        model="qwen3.5:397b-cloud",
        base_url="https://ollama.com/api",
        transport=transport,
    )

    with pytest.raises(ProviderError) as excinfo:
        adapter.complete(_structured_prompt())

    assert excinfo.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert len(transport.calls) == 3  # max_attempts default


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
    transport = FakeTransport([{"message": {"role": "assistant", "content": fenced}}])
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
    transport = FakeTransport([{"message": {"role": "assistant", "content": preamble}}])
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
    transport = FakeTransport([{"message": {"role": "assistant", "content": garbage}}])
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )

    with pytest.raises(ProviderError, match="Raw \\(first 300 chars\\)") as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert exc_info.value.response_text == garbage


def test_ollama_adapter_sets_num_predict_and_num_ctx() -> None:
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": '{"assistant_message":"ok","actions":[]}'}}]
    )
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        max_output_tokens=2048,
        num_ctx=8192,
        transport=transport,
    )

    adapter.complete(_structured_prompt())

    options = transport.calls[0]["payload"]["options"]
    assert options["num_predict"] == 2048
    assert options["num_ctx"] == 8192
    assert options["temperature"] == 0


def test_ollama_adapter_raises_truncated_on_done_reason_length() -> None:
    """A length-truncated response surfaces a distinct TRUNCATED error, not a JSON parse error."""
    partial = '{"assistant_message":"writing","actions":[{"id":"w","kind":"tool_call"'
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": partial}, "done_reason": "length"}]
    )
    adapter = OllamaChatAdapter(
        model="kimi-k2.6:cloud",
        base_url="http://localhost:11434/api",
        max_output_tokens=128,
        transport=transport,
    )

    with pytest.raises(ProviderError, match="truncated") as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.TRUNCATED
    assert exc_info.value.response_text == partial


def test_openai_adapter_sets_max_output_tokens() -> None:
    transport = FakeTransport(
        [{"id": "r", "output_text": '{"assistant_message":"ok","actions":[]}'}]
    )
    adapter = OpenAIResponsesAdapter(
        model="gpt-5-mini",
        api_key="sk-test",
        max_output_tokens=4096,
        transport=transport,
    )

    adapter.complete(_structured_prompt())

    assert transport.calls[0]["payload"]["max_output_tokens"] == 4096


def test_openai_adapter_raises_truncated_on_incomplete_response() -> None:
    transport = FakeTransport(
        [
            {
                "id": "resp_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": '{"assistant_message":"partial"',
            }
        ]
    )
    adapter = OpenAIResponsesAdapter(
        model="gpt-5-mini",
        api_key="sk-test",
        transport=transport,
    )

    with pytest.raises(ProviderError, match="truncated") as exc_info:
        adapter.complete(_structured_prompt())

    assert exc_info.value.code is ProviderErrorCode.TRUNCATED


def test_ollama_adapter_honors_prompt_temperature() -> None:
    transport = FakeTransport(
        [{"message": {"role": "assistant", "content": '{"assistant_message":"ok","actions":[]}'}}]
    )
    adapter = OllamaChatAdapter(
        model="glm-5.1:cloud",
        base_url="http://localhost:11434/api",
        transport=transport,
    )
    prompt = ProviderPrompt(
        messages=[ProviderMessage(role=ProviderMessageRole.USER, content="Plan this.")],
        response_format=ProviderResponseFormat.JSON_OBJECT,
        schema_name="assistant_plan",
        output_schema={"type": "object"},
        temperature=0.4,
    )

    adapter.complete(prompt)

    assert transport.calls[0]["payload"]["options"]["temperature"] == 0.4
