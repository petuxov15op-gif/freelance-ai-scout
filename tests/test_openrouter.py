import json

import httpx
import pytest

from scout.ai import OpenRouter, draft_problem, validate_response
from scout.portfolio import relevant_cases


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
@pytest.mark.parametrize("announcement,case_id,expected_link", [
    ("Нужен простой сайт, без Тильды", None, False),
    ("Нужен простой сайт, без Тильды", "null", False),
    ("Нужен Telegram-бот для отслеживания объявлений в каналах", "freelance_ai_scout", True),
])
async def test_draft_uses_verified_case_only_when_selected(repo, announcement, case_id, expected_link):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["response_format"]["json_schema"]["schema"]["required"] == ["draft", "case_id"]
        assert payload["temperature"] == 0.4
        assert "Не додумывай сферу заказчика" in payload["messages"][0]["content"]
        system = payload["messages"][0]["content"]
        assert "пекарн" not in system.lower()
        assert ("https://github.com/petuxov15op-gif/freelance-ai-scout" in system) is expected_link
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "draft": "Здравствуйте! Могу помочь с сайтом.", "case_id": case_id,
        })}}]})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        draft = (await ai.ask("draft", announcement))["draft"]
        assert ("https://github.com/petuxov15op-gif/freelance-ai-scout" in draft) is expected_link
    finally:
        await ai.close()


def test_draft_rejects_unverified_link():
    with pytest.raises(ValueError):
        validate_response({"draft": "Вот работа: https://example.com", "case_id": None}, "draft")


@pytest.mark.parametrize("draft", [
    "Напишите на client@example.com",
    "Позвоните +7 999 123-45-67",
    "Напиши в личку",
])
def test_draft_rejects_customer_contact_and_informal_address(draft):
    assert draft_problem(draft)


def test_unrelated_announcement_has_no_portfolio_context():
    assert relevant_cases("НУЖЕН НЕДОРОГОЙ ПРОСТОЙ САЙТ, НЕ ТИЛЬДА") == {}
    assert "freelance_ai_scout" in relevant_cases(
        "Нужен Telegram-бот, который отслеживает объявления в каналах")


@pytest.mark.asyncio
async def test_draft_rejects_case_for_unrelated_site(repo):
    response = {"choices": [{"message": {"content": json.dumps({
        "draft": "Здравствуйте! Сайт сделаю.", "case_id": "freelance_ai_scout",
    })}}]}
    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)), base_url="https://openrouter.ai/api/v1")
    try:
        with pytest.raises(ValueError, match="неподходящим проектом"):
            await ai.ask("draft", "Нужен сайт")
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_draft_retries_contact_sent_back_to_customer(repo):
    replies = iter([
        "Привет! Готов взяться за разработку Telegram-бота. Напиши в личку @plashnya — обсудим задачи.",
        "Здравствуйте! Могу разработать Telegram-бота. Расскажите, какие задачи он должен решать?",
    ])

    def respond(request):
        payload = json.loads(request.content)
        assert "не предлагай заказчику написать" in payload["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "draft": next(replies), "case_id": None,
        })}}]})

    ai = OpenRouter("fake-key", "test-model", repo, 1)
    await ai.client.aclose()
    ai.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://openrouter.ai/api/v1")
    try:
        result = await ai.ask("draft", "Нужен Telegram-бот. Для отклика пишите @plashnya")
        assert result["draft"].startswith("Здравствуйте!")
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
