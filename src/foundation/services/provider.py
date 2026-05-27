"""Provider adapter contracts and supported provider implementations."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from foundation.models import (
    ProviderPrompt,
    ProviderResponse,
    ProviderResponseFormat,
    ProviderResponseMetadata,
    ProviderUsage,
)
from foundation.observability import (
    EVENT_PROVIDER_CALL_FAILED,
    EVENT_PROVIDER_CALL_FINISHED,
    EVENT_PROVIDER_CALL_RETRY,
    EVENT_PROVIDER_CALL_STARTED,
    emit_event,
    emit_exception,
)
from foundation.settings import AppSettings, SecretResolutionStatus

logger = logging.getLogger("foundation.services.provider")


class ProviderErrorCode(StrEnum):
    """Normalized provider failure categories."""

    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"
    TRUNCATED = "truncated"
    REFUSAL = "refusal"
    UNSUPPORTED_PROVIDER = "unsupported_provider"


class ProviderError(RuntimeError):
    """Raised when a provider request fails before orchestration can continue."""

    def __init__(
        self,
        message: str,
        *,
        code: ProviderErrorCode,
        retryable: bool = False,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.response_text = response_text


class ProviderAdapter(Protocol):
    """Common provider interface used by the Stage 5 orchestrator."""

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        """Submit a prompt and return normalized output."""


class JsonTransport(Protocol):
    """Minimal HTTP transport contract to keep the adapter testable."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """POST a JSON payload and return a decoded JSON object."""


class UrllibJsonTransport:
    """Stdlib JSON transport for the OpenAI responses endpoint."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            url=url,
            data=data,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = _provider_error_message(body) or (
                f"Provider request failed with HTTP {exc.code}."
            )
            if exc.code in {401, 403}:
                raise ProviderError(
                    message,
                    code=ProviderErrorCode.AUTHENTICATION,
                    status_code=exc.code,
                    response_text=body,
                ) from exc
            if exc.code == 429:
                raise ProviderError(
                    message,
                    code=ProviderErrorCode.RATE_LIMIT,
                    retryable=True,
                    status_code=exc.code,
                    response_text=body,
                ) from exc
            if exc.code in {408, 409} or exc.code >= 500:
                raise ProviderError(
                    message,
                    code=ProviderErrorCode.SERVER_ERROR,
                    retryable=True,
                    status_code=exc.code,
                    response_text=body,
                ) from exc
            raise ProviderError(
                message,
                code=ProviderErrorCode.BAD_REQUEST,
                status_code=exc.code,
                response_text=body,
            ) from exc
        except (TimeoutError, OSError, urllib_error.URLError) as exc:
            raise ProviderError(
                f"Provider network request failed: {exc}",
                code=ProviderErrorCode.NETWORK,
                retryable=True,
            ) from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider returned a non-JSON response.",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=body,
            ) from exc
        if not isinstance(decoded, dict):
            raise ProviderError(
                "Provider returned a JSON response with an unexpected shape.",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=body,
            )
        return decoded


def _provider_error_message(body: str) -> str | None:
    if not body.strip():
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if not isinstance(payload, dict):
        return None
    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
        message = error_payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


class OpenAIResponsesAdapter:
    """OpenAI responses API adapter with retry handling and JSON-mode support."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int = 60,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        max_output_tokens: int | None = None,
        transport: JsonTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport or UrllibJsonTransport()

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        started_at = time.monotonic()
        endpoint = f"{self._base_url}/responses"
        payload = self._build_payload(prompt)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }
        attempt_started_at = started_at

        logger.info(
            "provider_request_started provider=openai model=%s format=%s",
            self._model,
            prompt.response_format.value,
        )

        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                emit_event(
                    EVENT_PROVIDER_CALL_STARTED,
                    payload={
                        "provider": "openai",
                        "model": self._model,
                        "attempt": attempt,
                        "prompt_message_count": len(prompt.messages),
                        "response_format": prompt.response_format.value,
                    },
                    logger_name="foundation.services.provider",
                )
                response_payload = self._transport.post_json(
                    url=endpoint,
                    headers=headers,
                    payload=payload,
                    timeout_seconds=self._timeout_seconds,
                )
                content = self._extract_content(response_payload)
                structured_output: dict[str, Any] | None = None
                if prompt.response_format is ProviderResponseFormat.JSON_OBJECT:
                    structured_output = self._parse_json_object(content)

                metadata = ProviderResponseMetadata(
                    provider="openai",
                    model=self._model,
                    response_id=_coerce_optional_string(response_payload.get("id")),
                    latency_seconds=time.monotonic() - started_at,
                    attempts=attempt,
                    usage=_parse_usage(response_payload.get("usage")),
                )
                logger.info(
                    "provider_request_finished provider=openai model=%s attempts=%s latency=%.3f",
                    self._model,
                    attempt,
                    metadata.latency_seconds,
                )
                emit_event(
                    EVENT_PROVIDER_CALL_FINISHED,
                    payload={
                        "provider": "openai",
                        "model": self._model,
                        "attempt": attempt,
                        "latency_seconds": metadata.latency_seconds,
                    },
                    logger_name="foundation.services.provider",
                )
                return ProviderResponse(
                    content=content,
                    structured_output=structured_output,
                    metadata=metadata,
                )
            except ProviderError as exc:
                last_error = exc
                retry_requested = exc.retryable and attempt < self._max_attempts
                logger.warning(
                    (
                        "provider_request_failed provider=openai model=%s "
                        "attempt=%s code=%s retryable=%s"
                    ),
                    self._model,
                    attempt,
                    exc.code.value,
                    exc.retryable,
                )
                if not retry_requested:
                    emit_exception(
                        EVENT_PROVIDER_CALL_FAILED,
                        exc,
                        payload={
                            "provider": "openai",
                            "model": self._model,
                            "attempt": attempt,
                            "code": exc.code.value,
                            "retryable": exc.retryable,
                            "status_code": exc.status_code,
                            "latency_seconds": time.monotonic() - attempt_started_at,
                        },
                        logger_name="foundation.services.provider",
                    )
                    raise
                emit_event(
                    EVENT_PROVIDER_CALL_RETRY,
                    payload={
                        "provider": "openai",
                        "model": self._model,
                        "attempt": attempt,
                        "code": exc.code.value,
                        "status_code": exc.status_code,
                    },
                    logger_name="foundation.services.provider",
                )
                attempt_started_at = time.monotonic()
                time.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))

        assert last_error is not None
        raise last_error

    def _build_payload(self, prompt: ProviderPrompt) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": [
                {
                    "role": message.role.value,
                    "content": message.content,
                }
                for message in prompt.messages
            ],
        }
        if self._max_output_tokens is not None:
            payload["max_output_tokens"] = self._max_output_tokens
        if prompt.response_format is ProviderResponseFormat.JSON_OBJECT:
            assert prompt.schema_name is not None
            assert prompt.output_schema is not None
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": prompt.schema_name,
                    "schema": prompt.output_schema,
                    "strict": True,
                }
            }
        return payload

    def _extract_content(self, payload: Mapping[str, Any]) -> str:
        # An incomplete response means the model hit max_output_tokens; the
        # output is partial.  Flag it explicitly rather than parsing truncated JSON.
        if payload.get("status") == "incomplete":
            details = payload.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, Mapping) else None
            if reason in (None, "max_output_tokens"):
                raise ProviderError(
                    "Provider response was truncated before completion "
                    f"(status=incomplete, reason={reason}). Raise provider.max_output_tokens, "
                    "or have the planner emit a smaller plan (use content_brief for large "
                    "file bodies).",
                    code=ProviderErrorCode.TRUNCATED,
                    response_text=_coerce_optional_string(payload.get("output_text")),
                )

        top_level_text = payload.get("output_text")
        if isinstance(top_level_text, str) and top_level_text.strip():
            return top_level_text.strip()

        refusal_message: str | None = None
        content_parts: list[str] = []
        raw_output = payload.get("output", [])
        if isinstance(raw_output, list):
            for item in raw_output:
                if not isinstance(item, Mapping):
                    continue
                raw_content = item.get("content", [])
                if not isinstance(raw_content, list):
                    continue
                for content_item in raw_content:
                    if not isinstance(content_item, Mapping):
                        continue
                    content_type = content_item.get("type")
                    if content_type in {"output_text", "text"}:
                        text = content_item.get("text")
                        if isinstance(text, str):
                            content_parts.append(text)
                    elif content_type == "refusal":
                        refusal_value = content_item.get("refusal") or content_item.get("text")
                        if isinstance(refusal_value, str) and refusal_value.strip():
                            refusal_message = refusal_value.strip()

        if refusal_message:
            raise ProviderError(
                refusal_message,
                code=ProviderErrorCode.REFUSAL,
            )

        combined = "".join(content_parts).strip()
        if combined:
            return combined

        raise ProviderError(
            "Provider returned an empty response.",
            code=ProviderErrorCode.INVALID_RESPONSE,
        )

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        cleaned = _try_extract_json(text)
        logger.debug("parse_json_object raw_text=%s", text[:500])
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider returned invalid JSON for a structured response. "
                f"Raw (first 300 chars): {text[:300]!r}",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Provider returned structured output that was not a JSON object. "
                f"Raw (first 300 chars): {text[:300]!r}",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            )
        return payload


class OllamaChatAdapter:
    """Ollama chat API adapter with retry handling and structured output support."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "http://localhost:11434/api",
        timeout_seconds: int = 60,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        max_output_tokens: int | None = None,
        num_ctx: int | None = None,
        transport: JsonTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_output_tokens = max_output_tokens
        self._num_ctx = num_ctx
        self._transport = transport or UrllibJsonTransport()

    def complete(self, prompt: ProviderPrompt) -> ProviderResponse:
        started_at = time.monotonic()
        endpoint = self._base_url if self._base_url.endswith("/chat") else f"{self._base_url}/chat"
        payload = self._build_payload(prompt)
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt_started_at = started_at

        logger.info(
            "provider_request_started provider=ollama model=%s format=%s",
            self._model,
            prompt.response_format.value,
        )

        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                emit_event(
                    EVENT_PROVIDER_CALL_STARTED,
                    payload={
                        "provider": "ollama",
                        "model": self._model,
                        "attempt": attempt,
                        "prompt_message_count": len(prompt.messages),
                        "response_format": prompt.response_format.value,
                    },
                    logger_name="foundation.services.provider",
                )
                response_payload = self._transport.post_json(
                    url=endpoint,
                    headers=headers,
                    payload=payload,
                    timeout_seconds=self._timeout_seconds,
                )
                logger.debug(
                    "ollama_raw_response keys=%s payload=%s",
                    list(response_payload.keys()),
                    json.dumps(response_payload, default=str)[:2000],
                )
                content = self._extract_content(
                    response_payload,
                    response_format=prompt.response_format,
                )
                structured_output: dict[str, Any] | None = None
                if prompt.response_format is ProviderResponseFormat.JSON_OBJECT:
                    structured_output = self._parse_json_object(content)

                metadata = ProviderResponseMetadata(
                    provider="ollama",
                    model=self._model,
                    response_id=_coerce_optional_string(response_payload.get("id")),
                    latency_seconds=time.monotonic() - started_at,
                    attempts=attempt,
                    usage=_parse_ollama_usage(response_payload),
                )
                logger.info(
                    "provider_request_finished provider=ollama model=%s attempts=%s latency=%.3f",
                    self._model,
                    attempt,
                    metadata.latency_seconds,
                )
                emit_event(
                    EVENT_PROVIDER_CALL_FINISHED,
                    payload={
                        "provider": "ollama",
                        "model": self._model,
                        "attempt": attempt,
                        "latency_seconds": metadata.latency_seconds,
                    },
                    logger_name="foundation.services.provider",
                )
                return ProviderResponse(
                    content=content,
                    structured_output=structured_output,
                    metadata=metadata,
                )
            except ProviderError as exc:
                last_error = exc
                retry_requested = exc.retryable and attempt < self._max_attempts
                logger.warning(
                    (
                        "provider_request_failed provider=ollama model=%s "
                        "attempt=%s code=%s retryable=%s"
                    ),
                    self._model,
                    attempt,
                    exc.code.value,
                    exc.retryable,
                )
                if not retry_requested:
                    emit_exception(
                        EVENT_PROVIDER_CALL_FAILED,
                        exc,
                        payload={
                            "provider": "ollama",
                            "model": self._model,
                            "attempt": attempt,
                            "code": exc.code.value,
                            "retryable": exc.retryable,
                            "status_code": exc.status_code,
                            "latency_seconds": time.monotonic() - attempt_started_at,
                        },
                        logger_name="foundation.services.provider",
                    )
                    raise
                emit_event(
                    EVENT_PROVIDER_CALL_RETRY,
                    payload={
                        "provider": "ollama",
                        "model": self._model,
                        "attempt": attempt,
                        "code": exc.code.value,
                        "status_code": exc.status_code,
                    },
                    logger_name="foundation.services.provider",
                )
                attempt_started_at = time.monotonic()
                time.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _ollama_role(role: str) -> str:
        """Map provider roles to Ollama-supported roles.

        Ollama only accepts system, user, and assistant.
        The OpenAI 'developer' role is equivalent to 'system'.
        """
        if role == "developer":
            return "system"
        return role

    @staticmethod
    def _needs_think_for_structured_output(model: str) -> bool:
        # Qwen 3.x is the only family we've confirmed needs think=true
        # with format=<schema>; other thinking models (e.g. deepseek-v3.2)
        # regress into free-form thinking when think=true is forced.
        return model.lower().startswith("qwen3")

    def _build_payload(self, prompt: ProviderPrompt) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": self._ollama_role(message.role.value),
                    "content": message.content,
                }
                for message in prompt.messages
            ],
            "stream": False,
        }
        options: dict[str, Any] = {}
        if self._max_output_tokens is not None:
            options["num_predict"] = self._max_output_tokens
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx
        if prompt.response_format is ProviderResponseFormat.JSON_OBJECT:
            assert prompt.output_schema is not None
            payload["format"] = prompt.output_schema
            options["temperature"] = 0
            if self._needs_think_for_structured_output(self._model):
                payload["think"] = True
        if options:
            payload["options"] = options
        return payload

    def _extract_content(
        self,
        payload: Mapping[str, Any],
        *,
        response_format: ProviderResponseFormat,
    ) -> str:
        json_requested = response_format is ProviderResponseFormat.JSON_OBJECT

        # A truncated generation (model hit its output-token budget) leaves the
        # JSON object unterminated.  Surface that explicitly here instead of
        # letting it fall through to a confusing json.loads failure downstream.
        if payload.get("done_reason") == "length":
            message = payload.get("message")
            partial = ""
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                partial = message["content"]
            raise ProviderError(
                "Provider response was truncated before completion (done_reason=length). "
                "The model hit its output-token limit, so the response is incomplete. "
                "Raise provider.max_output_tokens, or have the planner emit a smaller plan "
                "(use content_brief for large file bodies).",
                code=ProviderErrorCode.TRUNCATED,
                response_text=partial,
            )

        # Standard Ollama local format: {"message": {"content": "...", "thinking": "..."}}
        message = payload.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            # For free-form calls, thinking is an acceptable fallback when
            # content is empty.  For JSON_OBJECT calls it is NOT — thinking
            # is reasoning narrative, never schema-constrained output — so
            # we skip the fallback and surface a clear error instead.
            if not json_requested:
                thinking = message.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    return thinking.strip()

        # OpenAI-compatible format used by some Ollama cloud endpoints:
        # {"choices": [{"message": {"content": "..."}}]}
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, Mapping):
                choice_message = first_choice.get("message")
                if isinstance(choice_message, Mapping):
                    content = choice_message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

        # Surface the actual response shape to help diagnose format mismatches.
        keys = list(payload.keys())
        msg_detail = ""
        thinking_seen = False
        if isinstance(message, Mapping):
            msg_content = message.get("content")
            msg_thinking = message.get("thinking")
            thinking_seen = isinstance(msg_thinking, str) and bool(msg_thinking.strip())
            msg_detail = f" message.content={msg_content!r} message.thinking={msg_thinking!r}"
        eval_count = payload.get("eval_count")
        prompt_eval_count = payload.get("prompt_eval_count")
        if json_requested and thinking_seen:
            error_msg = (
                "Provider produced only thinking tokens and no structured JSON "
                "output. This usually means the model emitted reasoning "
                "narrative instead of honoring the requested schema"
                f" (response keys: {keys},"
                f" eval_count={eval_count},"
                f" prompt_eval_count={prompt_eval_count},"
                f"{msg_detail})."
            )
        else:
            error_msg = (
                f"Provider returned an empty chat response"
                f" (response keys: {keys},"
                f" eval_count={eval_count},"
                f" prompt_eval_count={prompt_eval_count},"
                f"{msg_detail})."
            )
        raise ProviderError(
            error_msg,
            code=ProviderErrorCode.INVALID_RESPONSE,
            response_text=json.dumps(payload, default=str)[:2000],
        )

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        cleaned = _try_extract_json(text)
        logger.debug("parse_json_object raw_text=%s", text[:500])
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider returned invalid JSON for a structured response. "
                f"Raw (first 300 chars): {text[:300]!r}",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Provider returned structured output that was not a JSON object. "
                f"Raw (first 300 chars): {text[:300]!r}",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            )
        return payload


_CODE_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def _try_extract_json(text: str) -> str:
    """Best-effort extraction of a JSON object from model output.

    Many models wrap valid JSON in markdown code fences or include
    conversational preamble.  This helper tries common patterns before
    falling back to the original text so ``json.loads`` can report the
    real error.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped

    # Try markdown code-fence extraction (```json ... ``` or ``` ... ```)
    match = _CODE_FENCE_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # Greedy brace extraction: first '{' to last '}'
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        return stripped[first : last + 1]

    return stripped


def _coerce_optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_usage(value: object) -> ProviderUsage | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    total_tokens = value.get("total_tokens")
    if input_tokens is not None and not isinstance(input_tokens, int):
        input_tokens = None
    if output_tokens is not None and not isinstance(output_tokens, int):
        output_tokens = None
    if total_tokens is not None and not isinstance(total_tokens, int):
        total_tokens = None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _parse_ollama_usage(payload: Mapping[str, Any]) -> ProviderUsage | None:
    input_tokens = payload.get("prompt_eval_count")
    output_tokens = payload.get("eval_count")
    if input_tokens is not None and not isinstance(input_tokens, int):
        input_tokens = None
    if output_tokens is not None and not isinstance(output_tokens, int):
        output_tokens = None
    if input_tokens is None and output_tokens is None:
        return None
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def build_provider_adapter(
    settings: AppSettings,
    *,
    transport: JsonTransport | None = None,
) -> ProviderAdapter:
    """Build the configured provider adapter for Stage 5."""
    provider_name = settings.provider.normalized_name()
    if provider_name not in {"openai", "ollama"}:
        raise ProviderError(
            (
                f"Provider {settings.provider.name!r} is not supported in Foundation CLI v0.1. "
                "Supported providers: openai, ollama."
            ),
            code=ProviderErrorCode.UNSUPPORTED_PROVIDER,
        )

    resolution = settings.provider.resolve_api_key(
        environment=settings.provider_environment(),
    )
    api_key: str | None = None
    if resolution.status is SecretResolutionStatus.RESOLVED and resolution.value is not None:
        api_key = resolution.value.get_secret_value()
    elif settings.provider.credentials_required():
        raise ProviderError(
            resolution.detail,
            code=ProviderErrorCode.AUTHENTICATION,
        )

    if provider_name == "ollama":
        return OllamaChatAdapter(
            model=settings.provider.model,
            api_key=api_key,
            base_url=settings.provider.effective_base_url(),
            timeout_seconds=settings.provider.request_timeout_seconds,
            max_output_tokens=settings.provider.max_output_tokens,
            num_ctx=settings.provider.num_ctx,
            transport=transport,
        )

    return OpenAIResponsesAdapter(
        model=settings.provider.model,
        api_key=api_key or "",
        base_url=settings.provider.effective_base_url(),
        timeout_seconds=settings.provider.request_timeout_seconds,
        max_output_tokens=settings.provider.max_output_tokens,
        transport=transport,
    )
