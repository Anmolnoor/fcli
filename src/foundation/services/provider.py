"""Provider adapter contracts and supported provider implementations."""

from __future__ import annotations

import json
import logging
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
        transport: JsonTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
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
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider returned invalid JSON for a structured response.",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Provider returned structured output that was not a JSON object.",
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
        transport: JsonTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
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
                content = self._extract_content(response_payload)
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

    def _build_payload(self, prompt: ProviderPrompt) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                }
                for message in prompt.messages
            ],
            "stream": False,
        }
        if prompt.response_format is ProviderResponseFormat.JSON_OBJECT:
            assert prompt.output_schema is not None
            payload["format"] = prompt.output_schema
            payload["options"] = {
                "temperature": 0,
            }
        return payload

    def _extract_content(self, payload: Mapping[str, Any]) -> str:
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError(
                "Provider returned a chat response without a message payload.",
                code=ProviderErrorCode.INVALID_RESPONSE,
            )

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

        raise ProviderError(
            "Provider returned an empty chat response.",
            code=ProviderErrorCode.INVALID_RESPONSE,
        )

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider returned invalid JSON for a structured response.",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Provider returned structured output that was not a JSON object.",
                code=ProviderErrorCode.INVALID_RESPONSE,
                response_text=text,
            )
        return payload


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
            transport=transport,
        )

    return OpenAIResponsesAdapter(
        model=settings.provider.model,
        api_key=api_key or "",
        base_url=settings.provider.effective_base_url(),
        timeout_seconds=settings.provider.request_timeout_seconds,
        transport=transport,
    )
