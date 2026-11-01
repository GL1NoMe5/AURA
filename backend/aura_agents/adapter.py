"""Async Baseten adapter for the AURA runtime agents (AGENT-001).

Fixed target only: https://inference.baseten.co/v1 and
meta-models/muse-spark-open-1026. SDK hidden retries are disabled; the
adapter performs at most one bounded retry for 429 / temporary 5xx /
transport failures with cancellable backoff. No auth or schema retries.
Errors are redacted categories — never keys, headers or raw messages.
Non-streaming structured completions only; streaming is not implemented.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from aura_api.schemas import AgentUsage

BASE_URL = "https://inference.baseten.co/v1"
MODEL = "meta-models/muse-spark-open-1026"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_TOKENS = 4096
DEFAULT_ATTEMPT_DEADLINE_SECONDS = 60.0
MAX_RESPONSE_CHARS = 32768
MAX_REASONING_CHARS = 8000
MAX_ATTEMPTS = 2  # initial attempt + at most one retry
RETRY_BACKOFF_SECONDS = 0.5


def load_api_key(project_root: Optional[Path] = None) -> Optional[str]:
    """Read the key from env or project-root .env. Never prints it."""
    value = os.environ.get("BASETEN_API_KEY", "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if project_root is not None:
        env_file = project_root / ".env"
        if env_file.is_file():
            try:
                from dotenv import dotenv_values

                file_value = dotenv_values(env_file).get("BASETEN_API_KEY")
            except Exception:
                return None
            if isinstance(file_value, str) and file_value.strip():
                return file_value.strip()
    return None


def categorize_error(exc: BaseException) -> tuple[str, Optional[int]]:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        resp_status = getattr(response, "status_code", None)
        status = resp_status if isinstance(resp_status, int) else None
    name = type(exc).__name__.lower()
    if status in (401, 403) or "auth" in name or "permission" in name:
        return "auth_error", status
    if status == 429 or "ratelimit" in name or "rate_limit" in name:
        return "rate_limit", status
    if "timeout" in name or status == 408:
        return "timeout", status
    if status is not None and status >= 500:
        return "server_error", status
    if "connection" in name or "transport" in name:
        return "connection_error", status
    if status is not None:
        return "request_error", status
    return "transport_error", status


def is_retryable(category: str) -> bool:
    return category in (
        "rate_limit", "server_error", "timeout", "connection_error",
        "transport_error")


class ProviderCallError(RuntimeError):
    def __init__(self, category: str, status_code: Optional[int],
                 attempts: int):
        super().__init__(f"provider call failed: {category}")
        self.category = category
        self.status_code = status_code
        self.attempts = attempts


class ProviderOutputError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(f"provider output rejected: {category}")
        self.category = category


@dataclass
class CallResult:
    content: Optional[str]
    reasoning: Optional[str]
    usage: Optional[AgentUsage]
    response_model: Optional[str]
    finish_reason: Optional[str]
    duration_ms: float
    attempts: int
    requested_model: str = MODEL
    reasoning_truncated: bool = False


def _field(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) is not None:
                return obj[name]
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for name in names:
            if extra.get(name) is not None:
                return extra[name]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:
            data = None
        if isinstance(data, dict):
            for name in names:
                if data.get(name) is not None:
                    return data[name]
    return None


def extract_usage(raw: Any) -> Optional[AgentUsage]:
    if raw is None:
        return None
    details = _field(raw, "completion_tokens_details") or {}
    usage = AgentUsage(
        prompt_tokens=_field(raw, "prompt_tokens"),
        completion_tokens=_field(raw, "completion_tokens"),
        total_tokens=_field(raw, "total_tokens"),
        reasoning_tokens=_field(details, "reasoning_tokens"),
    )
    if all(value is None for value in (
            usage.prompt_tokens, usage.completion_tokens,
            usage.total_tokens, usage.reasoning_tokens)):
        return None
    return usage


def parse_response(raw: Any, duration_ms: float, attempts: int) -> CallResult:
    choices = _field(raw, "choices") or []
    choice = choices[0] if choices else None
    message = _field(choice, "message") if choice is not None else None
    content = _field(message, "content")
    reasoning = _field(message, "reasoning_content", "reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = None
    reasoning_truncated = bool(
        reasoning is not None and len(reasoning) > MAX_REASONING_CHARS)
    if reasoning_truncated:
        # Bound stored reasoning; the flag keeps the truncation honest
        # instead of dropping an otherwise valid structured result.
        reasoning = reasoning[:MAX_REASONING_CHARS]
    return CallResult(
        content=content if isinstance(content, str) else None,
        reasoning=reasoning,
        usage=extract_usage(_field(raw, "usage")),
        response_model=_field(raw, "model"),
        finish_reason=_field(choice, "finish_reason"),
        duration_ms=duration_ms, attempts=attempts,
        reasoning_truncated=reasoning_truncated,
    )


async def _await_cancellable(awaitable, cancel_event: Optional[asyncio.Event]):
    """Await while honouring cancel_event; cancels the inner task."""
    task = asyncio.ensure_future(awaitable)
    if cancel_event is None:
        return await task
    cancel_wait = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_wait in done and cancel_event.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cancel_wait.cancel()
        await asyncio.gather(cancel_wait, return_exceptions=True)


class BasetenAdapter:
    """One async client, fixed model/URL, bounded retry, clean shutdown."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        project_root: Optional[Path] = None,
        client: Any = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        backoff_seconds: float = RETRY_BACKOFF_SECONDS,
        attempt_deadline_seconds: float = DEFAULT_ATTEMPT_DEADLINE_SECONDS,
    ) -> None:
        self._api_key = api_key if api_key is not None else load_api_key(
            project_root)
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._backoff = backoff_seconds
        self._attempt_deadline = attempt_deadline_seconds
        self._client = client
        self._client_factory = client_factory
        self._closed = False
        self.dispatched_calls = 0
        self.dispatched_attempts = 0

    @property
    def ready(self) -> bool:
        return bool(self._api_key) and not self._closed

    def _ensure_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(self._api_key)
            else:
                from openai import AsyncOpenAI

                self._client = AsyncOpenAI(
                    api_key=self._api_key, base_url=BASE_URL,
                    timeout=self._timeout, max_retries=0)
        return self._client

    async def aclose(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def complete(
        self,
        messages: list[dict],
        *,
        cancel_event: Optional[asyncio.Event] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> CallResult:
        """One bounded non-streaming completion with at most one retry."""
        if reasoning_effort is not None and reasoning_effort not in ("low", "medium", "high", "xhigh"):
            raise ValueError("Unsupported Muse Spark Open reasoning effort")
        options = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        if not self.ready:
            raise ProviderCallError("missing_key", None, 0)
        client = self._ensure_client()
        started = time.perf_counter()
        self.dispatched_calls += 1
        attempts = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError()
            attempts += 1
            self.dispatched_attempts += 1
            try:
                # Strict wall-clock deadline per attempt, in addition to
                # the HTTP client's inactivity timeout.
                raw = await asyncio.wait_for(
                    _await_cancellable(
                        client.chat.completions.create(
                            model=MODEL, messages=messages,
                            max_tokens=max_tokens or self._max_tokens,
                            stream=False, **options),
                        cancel_event),
                    timeout=self._attempt_deadline)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                if attempts < MAX_ATTEMPTS:
                    await _await_cancellable(
                        asyncio.sleep(self._backoff), cancel_event)
                    continue
                raise ProviderCallError("timeout", None, attempts) from None
            except Exception as exc:
                category, status = categorize_error(exc)
                if is_retryable(category) and attempts < MAX_ATTEMPTS:
                    await _await_cancellable(
                        asyncio.sleep(self._backoff), cancel_event)
                    continue
                raise ProviderCallError(
                    category, status, attempts) from None
            duration_ms = (time.perf_counter() - started) * 1000.0
            result = parse_response(raw, round(duration_ms, 3), attempts)
            if result.content is not None \
                    and len(result.content) > MAX_RESPONSE_CHARS:
                raise ProviderOutputError("response_too_large")
            return result
