import asyncio
import json

import httpx
import pytest

from fdm.llm import LLMAuthError, LLMError, GroqClient, parse_json_object

MESSAGES = [{"role": "user", "content": "hi"}]


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                  "finish_reason": "stop"}]})


def error(status: int, message: str, code: str = "", **headers: str) -> httpx.Response:
    body = {"error": {"message": message, "type": "invalid_request_error", **({"code": code} if code else {})}}
    return httpx.Response(status, json=body, headers=headers)


def run(handler, models=("big", "small"), **kwargs):
    calls: list[str] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return handler(model, len(calls))

    async def go():
        options = {"backoff": 0, "cooldown": 0, **kwargs}
        async with GroqClient("key", models, transport=httpx.MockTransport(recording_handler), **options) as llm:
            return await llm.complete(MESSAGES, parse=parse_json_object)

    return asyncio.run(go()), calls


def test_request_uses_groq_chat_completions_with_json_mode():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok('{"ok": true}')

    async def go():
        async with GroqClient("gsk_test", ["big"], transport=httpx.MockTransport(handler)) as llm:
            return await llm.complete(MESSAGES, parse=parse_json_object, json_mode=True, temperature=0.5)

    assert asyncio.run(go()) == ({"ok": True}, "big")
    request = seen[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer gsk_test"
    assert json.loads(request.content) == {"model": "big", "messages": MESSAGES, "temperature": 0.5,
                                           "response_format": {"type": "json_object"}}


def test_missing_api_key_is_an_auth_error():
    with pytest.raises(LLMAuthError, match="GROQ_API_KEY"):
        GroqClient("", ["big"])


@pytest.mark.parametrize("status", [429, 413])
def test_rate_or_size_limit_falls_back_to_smaller_model(status):
    def handler(model, _):
        if model == "big":
            return error(status, "Rate limit reached for model `big` on tokens per minute (TPM)", "rate_limit_exceeded")
        return ok('{"ok": true}')

    (value, model), calls = run(handler)
    assert value == {"ok": True}
    assert model == "small"
    assert calls == ["big", "small"]


def test_context_length_falls_back():
    def handler(model, _):
        if model == "big":
            return error(400, "Please reduce the length of the messages or completion.", "context_length_exceeded")
        return ok('{"ok": 1}')

    (_, model), calls = run(handler)
    assert model == "small"
    assert calls == ["big", "small"]


def test_transient_error_retries_same_model_first():
    def handler(model, n):
        return error(503, "Service Unavailable") if n == 1 else ok('{"ok": 1}')

    (_, model), calls = run(handler)
    assert model == "big"
    assert calls == ["big", "big"]


def test_json_validate_failure_retries_same_model():
    def handler(model, n):
        if n == 1:
            return error(400, "Failed to generate JSON. Please adjust your prompt.", "json_validate_failed")
        return ok('{"ok": 1}')

    (_, model), calls = run(handler)
    assert model == "big"
    assert calls == ["big", "big"]


def test_unparseable_reply_moves_down_the_chain():
    def handler(model, _):
        return ok("sorry, no json here") if model == "big" else ok('```json\n{"ok": 2}\n```')

    (value, model), calls = run(handler, retries_per_model=0)
    assert value == {"ok": 2}
    assert calls == ["big", "small"]


@pytest.mark.parametrize("reply", [
    error(404, "The model `big` does not exist or you do not have access to it.", "model_not_found"),
    error(400, "The model `big` has been decommissioned and is no longer supported.", "model_decommissioned"),
])
def test_unavailable_model_is_skipped_for_later_calls(reply):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return reply if model == "big" else ok('{"ok": true}')

    async def go():
        async with GroqClient("key", ["big", "small"], transport=httpx.MockTransport(handler), backoff=0) as llm:
            first = await llm.complete(MESSAGES, parse=parse_json_object)
            second = await llm.complete(MESSAGES, parse=parse_json_object)
            return first, second, llm.unavailable

    first, second, unavailable = asyncio.run(go())
    assert first[1] == second[1] == "small"
    assert calls == ["big", "small", "small"]
    assert unavailable == {"big"}


def test_no_usable_models_gives_actionable_error():
    def handler(model, _):
        return error(404, f"The model `{model}` does not exist or you do not have access to it.", "model_not_found")

    with pytest.raises(LLMError, match="does not exist"):
        run(handler)


def test_auth_error_is_fatal():
    with pytest.raises(LLMAuthError, match="Invalid API Key"):
        run(lambda model, _: error(401, "Invalid API Key", "invalid_api_key"))


def test_everything_limited_retries_chain_once_then_fails():
    with pytest.raises(LLMError) as excinfo:
        run(lambda model, _: error(429, "Rate limit reached: requests per day (RPD)", "rate_limit_exceeded"))
    assert "small" in str(excinfo.value)


def test_cooldown_round_recovers():
    def handler(model, n):
        return error(429, "slow down", **{"retry-after": "0"}) if n <= 2 else ok('{"ok": 3}')

    (value, model), calls = run(handler)
    assert value == {"ok": 3}
    assert calls == ["big", "small", "big"]


def test_empty_completion_is_retried():
    def handler(model, n):
        return httpx.Response(200, json={"choices": []}) if n == 1 else ok('{"ok": 4}')

    (value, model), calls = run(handler)
    assert value == {"ok": 4} and calls == ["big", "big"]


def test_parse_json_object_variants():
    assert parse_json_object('Here you go: {"a": 1} thanks') == {"a": 1}
    assert parse_json_object('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
