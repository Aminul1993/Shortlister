import asyncio
import json

import httpx
import pytest

from fdm.llm import LLMAuthError, LLMError, OllamaCloud, parse_json_object

MESSAGES = [{"role": "user", "content": "hi"}]


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}, "done": True})


def run(handler, models=("big", "small"), **kwargs):
    calls: list[str] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return handler(model, len(calls))

    async def go():
        options = {"backoff": 0, "cooldown": 0, **kwargs}
        async with OllamaCloud("key", models, transport=httpx.MockTransport(recording_handler), **options) as llm:
            return await llm.complete(MESSAGES, parse=parse_json_object)

    return asyncio.run(go()), calls


def test_rate_limit_falls_back_to_smaller_model():
    def handler(model, _):
        return httpx.Response(429, json={"error": "rate limit exceeded"}) if model == "big" else ok('{"ok": true}')

    (value, model), calls = run(handler)
    assert value == {"ok": True}
    assert model == "small"
    assert calls == ["big", "small"]


def test_token_limit_falls_back():
    def handler(model, _):
        if model == "big":
            return httpx.Response(400, json={"error": "prompt is too long: exceeds context length"})
        return ok('{"ok": 1}')

    (_, model), calls = run(handler)
    assert model == "small"
    assert calls == ["big", "small"]


def test_transient_error_retries_same_model_first():
    def handler(model, n):
        return httpx.Response(503, json={"error": "overloaded"}) if n == 1 else ok('{"ok": 1}')

    (_, model), calls = run(handler)
    assert model == "big"
    assert calls == ["big", "big"]


def test_unparseable_reply_moves_down_the_chain():
    def handler(model, _):
        return ok("sorry, no json here") if model == "big" else ok('```json\n{"ok": 2}\n```')

    (value, model), calls = run(handler, retries_per_model=0)
    assert value == {"ok": 2}
    assert calls == ["big", "small"]


def test_model_outside_plan_is_skipped_for_later_calls():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "big":
            return httpx.Response(402, json={"error": "This model is not in the Free plan."})
        return ok('{"ok": true}')

    async def go():
        async with OllamaCloud("key", ["big", "small"], transport=httpx.MockTransport(handler), backoff=0) as llm:
            first = await llm.complete(MESSAGES, parse=parse_json_object)
            second = await llm.complete(MESSAGES, parse=parse_json_object)
            return first, second, llm.unavailable

    first, second, unavailable = asyncio.run(go())
    assert first[1] == second[1] == "small"
    assert calls == ["big", "small", "small"]
    assert unavailable == {"big"}


def test_no_usable_models_gives_actionable_error():
    def handler(model, _):
        return httpx.Response(404, json={"error": f"model '{model}' not found"})

    with pytest.raises(LLMError, match="not found"):
        run(handler)


def test_auth_error_is_fatal():
    with pytest.raises(LLMAuthError):
        run(lambda model, _: httpx.Response(401, json={"error": "unauthorized"}))


def test_everything_limited_retries_chain_once_then_fails():
    with pytest.raises(LLMError) as excinfo:
        run(lambda model, _: httpx.Response(429, json={"error": "weekly usage limit reached"}))
    assert "small" in str(excinfo.value)


def test_cooldown_round_recovers():
    def handler(model, n):
        return httpx.Response(429, json={"error": "slow down"}) if n <= 2 else ok('{"ok": 3}')

    (value, model), calls = run(handler)
    assert value == {"ok": 3}
    assert calls == ["big", "small", "big"]


def test_parse_json_object_variants():
    assert parse_json_object('Here you go: {"a": 1} thanks') == {"a": 1}
    assert parse_json_object('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
