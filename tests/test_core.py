import pytest

from scout.ai import validate_response
from scout.core import (calculate_score, explicit_ai_coding, explicit_it_task, likely_job, normalize_link,
                        has_direct_contact_link, mentions_platform, ongoing_role, potential_job, similar, text_hash)


def test_platform_names_and_direct_contact():
    assert mentions_platform("Нужен сайт, отклик только через FL.ru")
    assert mentions_platform("Нужен API, заказ на Freelancehunt.com")
    assert mentions_platform("Контакт заказчика — в LeadRadar")
    assert mentions_platform("https://freelance.ru/task/view/14746")
    assert not has_direct_contact_link("Контакт заказчика в LeadRadar. Подробнее на сайте")
    assert not has_direct_contact_link("Узнать больше в @SetJobs_bot")
    assert not has_direct_contact_link("Напишите в @job_developer", source_username="job_developer")
    assert has_direct_contact_link("Нужен бот", [{"label": "Написать заказчику", "url": "https://t.me/client12345"}])
    assert not has_direct_contact_link("Нужен бот", [{"label": "Подробнее", "url": "https://t.me/SetJobs_bot"}])


def test_relevance():
    assert likely_job("Нужен Telegram-бот для заявок, бюджет обсудим, срок около недели")
    assert not likely_job("Предлагаю свои услуги по разработке ботов и сайтов")
    assert potential_job("Voici le cahier des charges pour créer un site vitrine avec formulaire de contact")
    assert potential_job("Нужен бот на Python за 5000 ₽")


def test_it_task_and_permanent_ai_coding_signals():
    assert explicit_it_task("Нужен Python-скрипт для выгрузки заказов из API, оплата за результат")
    assert explicit_it_task("Нужно разработать Telegram-бота для заявок, бюджет обсудим")
    assert explicit_it_task("Требуется дизайн интерфейса мобильного приложения для заказа билетов")
    assert not explicit_it_task("Требуется чат-менеджер, работа удалённо, писать клиентам в Telegram")
    assert not explicit_it_task("Ищем людей для работы с объявлениями в цифровых сервисах")
    permanent = "Вакансия Python-разработчика на полную занятость, зарплата 150000 рублей"
    assert explicit_it_task(permanent)
    assert ongoing_role(permanent)
    assert not explicit_ai_coding(permanent)
    vibe = "Нужен Python-разработчик в штат для вайбкодинга с Codex, зарплата 150000 рублей"
    assert explicit_it_task(vibe)
    assert ongoing_role(vibe)
    assert explicit_ai_coding(vibe)
    assert explicit_it_task("Вакансия вайбкодера: делать проекты через Codex и Cursor")
    assert explicit_ai_coding("Работа на постоянку с помощью вайткодинга")
    assert not ongoing_role("Нужно сделать сайт для кафе за 20000 рублей")
    assert ongoing_role("Подработка: поддерживать Python-бота по 3 часа в день")
    assert ongoing_role("Частичная занятость: разработка сайта через Codex")
    assert not ongoing_role("Разовая подработка: написать Python-скрипт за один день")
    assert not explicit_it_task("Нужен SMM-специалист для публикации постов и контент-плана")


def test_dedupe_helpers():
    assert text_hash("Нужен бот!") == text_hash("нужен бот")
    assert similar("Нужен сайт для небольшой компании, оплата после обсуждения", "Нужен сайт для небольшой компании. Оплата после обсуждения!")
    assert normalize_link("http://T.ME/example/1?utm_source=x") == "https://t.me/example/1"


def test_score():
    assert calculate_score({"feasibility": 100, "financial": 80, "risk": 50, "clarity": 100, "timeline": 100}) == 85
    with pytest.raises(ValueError):
        calculate_score({"feasibility": 120})


def test_structured_response():
    assert validate_response({"is_job": True, "is_it": True, "codex_feasible": True,
                              "is_ongoing": False, "ai_coding_explicit": False,
                              "reason": "заказ"}, "prefilter")["is_job"]
    with pytest.raises(ValueError):
        validate_response({"is_job": "yes", "reason": "x"}, "prefilter")
    assert not validate_response({"suitable": False, "reason": "реклама"}, "candidate")["suitable"]


def test_deep_response_keeps_unknown_fields():
    result = {"title": "Сайт", "category": "разработка", "requirements": ["лендинг"],
              "budget": None, "currency": None, "deadline": None, "tools": ["Codex"],
              "ai_feasibility": "требуется ручная проверка", "knowledge": ["дизайн"],
              "risks": [], "extra_costs": [], "effort": None, "questions": ["Какой срок?"],
              "uncertainties": ["бюджет", "срок"], "score_reason": "требования частично ясны",
              "factors": {"feasibility": 80, "financial": 60, "risk": 70, "clarity": 60, "timeline": 50}}
    parsed = validate_response(result, "deep")
    assert parsed["budget"] is None
    assert parsed["score"] == 68
    with pytest.raises(ValueError):
        validate_response({**result, "factors": {"feasibility": 200}}, "deep")
