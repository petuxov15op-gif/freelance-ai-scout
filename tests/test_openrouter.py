import json

import httpx
import pytest

from scout.ai import OpenRouter


@pytest.mark.asyncio
async def test_openrouter_structured_and_usage(repo):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert {"is_job", "is_it", "codex_feasible", "is_ongoing", "ai_coding_explicit"} <= set(payload["response_format"]["json_schema"]["schema"]["required"])
        assert payload["provider"]["require_parameters"] is True
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"is_job": true, "is_it": true, "codex_feasible": true, "is_ongoing": false, "ai_coding_explicit": false, "reason": "заказ"}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        assert (await ai.ask("prefilter", "Нужен сайт"))["is_job"]
        assert repo.cost_today() == pytest.approx(.001)
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_openrouter_unavailable(repo):
    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, text="unsupported model")),
                                  base_url="https://openrouter.ai/api/v1")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await ai.ask("prefilter", "Нужен сайт")
    finally:
        await ai.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [(401, "недействителен"), (402, "Недостаточно средств"),
                                           (403, "запрещён"), (404, "не найдены")])
async def test_health_check_reports_openrouter_error(repo, code, expected):
    def respond(request):
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit_remaining": 2}})
        return httpx.Response(code, json={"error": {"code": code}})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        ok, detail = await ai.check_health()
        assert not ok
        assert expected in detail
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_health_check_verifies_key_and_model(repo):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"limit_remaining": 2.5}})
        assert json.loads(request.content)["model"] == "test-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        ok, detail = await ai.check_health()
        assert ok
        assert "$2.50" in detail
        assert calls == ["/api/v1/key", "/api/v1/chat/completions"]
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_health_check_does_not_call_model_after_key_limit(repo):
    def respond(request):
        assert request.url.path.endswith("/key")
        return httpx.Response(200, json={"data": {"limit_remaining": 0}})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        ok, detail = await ai.check_health()
        assert not ok
        assert "Лимит расходов" in detail
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_alternate_provider_keeps_openrouter_fields_out(repo):
    def respond(request):
        assert request.url.host == "example.test"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "claude-opus-5-5"
        assert payload["response_format"]["type"] == "json_schema"
        assert "provider" not in payload and "usage" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"suitable": true, "reason": "да"}'}}]})

    ai = OpenRouter("alternate-key", "claude-opus-5-5", repo, 1,
                    provider="compatible", base_url="https://example.test/v1")
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.test/v1")
    try:
        assert (await ai.ask("candidate", "Нужен бот"))["suitable"]
    finally:
        await ai.close()
