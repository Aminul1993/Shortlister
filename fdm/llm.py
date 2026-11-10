"""Groq chat client (OpenAI-compatible /chat/completions) with retries and fallback to smaller models.

Failure policy per model in the chain (largest first):
  401/403                            -> LLMAuthError immediately (a bad key fails every model)
  404, model_not_found/decommissioned -> next model, and skip this one for the client's lifetime
  413/429/498 or rate/token/context  -> next (smaller) model right away
  5xx, timeouts, unusable text       -> retry the same model with backoff, then next model
  other 4xx                          -> next model
If a whole pass failed only because of limits, the chain is retried once after a cooldown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

_LIMIT_HINTS = re.compile(
    r"rate.?limit|too many requests|quota|usage limit|tokens?|context.(?:length|window)|too long|exceed",
    re.I,
)
_UNAVAILABLE_CODES = {"model_not_found", "model_decommissioned"}


class LLMError(RuntimeError):
    """No model in the fallback chain produced a usable answer."""


class LLMAuthError(LLMError):
    """The API key was rejected; trying other models cannot help."""


class _Failure(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind  # "limit" | "transient" | "unavailable" | "model"
        self.retry_after = retry_after


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a reply that may be wrapped in prose or code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _error_detail(response: httpx.Response) -> tuple[str, str]:
    """(code, message) from an error reply shaped like {"error": {"message": ..., "code": ...}}."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code, message = "", error
    if isinstance(error, dict):
        code, message = str(error.get("code") or ""), error.get("message")
    return code, str(message or response.text or response.reason_phrase)[:300]


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(60.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


class GroqClient:
    def __init__(
        self,
        api_key: str,
        models: tuple[str, ...] | list[str],
        base_url: str = DEFAULT_BASE_URL,
        *,
        retries_per_model: int = 1,
        rounds: int = 2,
        backoff: float = 2.0,
        cooldown: float = 20.0,
        concurrency: int = 2,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise LLMAuthError("GROQ_API_KEY is not set")
        if not models:
            raise ValueError("at least one model is required")
        self.models = list(models)
        self.unavailable: set[str] = set()  # models this account cannot use (404, decommissioned)
        self._retries = retries_per_model
        self._rounds = rounds
        self._backoff = backoff
        self._cooldown = cooldown
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> GroqClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        parse: Callable[[str], T],
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> tuple[T, str]:
        """Return (parse(reply), model_used). `parse` raising ValueError counts as a bad reply.

        json_mode asks Groq for a JSON object. The messages must mention JSON and describe the fields wanted.
        """
        failures: list[str] = []
        async with self._semaphore:
            for round_no in range(self._rounds):
                limited, cooldown = False, 0.0
                for model in self.models:
                    if model in self.unavailable:
                        continue
                    for attempt in range(self._retries + 1):
                        try:
                            reply = await self._chat(model, messages, json_mode, temperature)
                            try:
                                return parse(reply), model
                            except ValueError as exc:
                                raise _Failure("transient", f"unusable reply: {exc}") from exc
                        except _Failure as failure:
                            failures.append(f"{model}: {failure}")
                            log.warning("Groq model %s failed (%s): %s", model, failure.kind, failure)
                            if failure.kind == "transient" and attempt < self._retries:
                                await asyncio.sleep(self._backoff * 2**attempt)
                                continue
                            if failure.kind == "limit":
                                limited = True
                                cooldown = max(cooldown, failure.retry_after or self._cooldown)
                            elif failure.kind == "unavailable":
                                self.unavailable.add(model)
                                log.warning("Skipping %s for the rest of this run; adjust GROQ_MODELS", model)
                            break
                if not limited or round_no == self._rounds - 1:
                    break
                log.warning("Every model is rate/token limited; cooling down for %.0fs", cooldown)
                await asyncio.sleep(cooldown)
        if not failures:
            raise LLMError(f"none of the configured models are available to this account: "
                           f"{', '.join(self.models)}. Set GROQ_MODELS to models your account can use.")
        raise LLMError("all Groq models failed: " + "; ".join(failures[-6:]))

    async def _chat(self, model: str, messages: list[dict[str, str]], json_mode: bool,
                    temperature: float) -> str:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise _Failure("transient", f"timeout: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise _Failure("transient", f"network error: {exc!r}") from exc

        status = response.status_code
        if status == 200:
            try:
                choice = (response.json().get("choices") or [{}])[0]
                content = ((choice.get("message") or {}).get("content") or "").strip()
            except (ValueError, AttributeError) as exc:
                raise _Failure("transient", "unreadable response body") from exc
            if not content:
                raise _Failure("transient", "empty completion")
            return content

        code, detail = _error_detail(response)
        if status in (401, 403):
            raise LLMAuthError(f"Groq rejected the API key (HTTP {status}): {detail}")
        if status == 404 or code in _UNAVAILABLE_CODES:
            raise _Failure("unavailable", f"HTTP {status}: {detail}")
        if code == "json_validate_failed":  # JSON mode caught a malformed reply; a retry usually works
            raise _Failure("transient", f"HTTP {status}: {detail}")
        if status in (413, 429, 498) or _LIMIT_HINTS.search(f"{code} {detail}"):
            raise _Failure("limit", f"HTTP {status}: {detail}", _retry_after(response))
        if status >= 500 or status == 408:
            raise _Failure("transient", f"HTTP {status}: {detail}")
        raise _Failure("model", f"HTTP {status}: {detail}")
