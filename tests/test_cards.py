from datetime import datetime, timezone
from html import unescape
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from scout.bot import OwnerMiddleware, create_router
from scout.cards import OrderCards, card_keyboard, card_text
from scout.repository import Repository
from scout.service import ScoutService


@pytest.fixture
def job(repo):
    channel = repo.candidate(901, "card_jobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    message = repo.save_message(channel.id, 55, "Нужен лендинг WordPress. Макет Figma готов.",
                                "https://t.me/card_jobs/55", datetime.now(timezone.utc), {})
    item, _ = repo.attach_job(message)
    repo.save_analysis(item.id, "deep", "test", {"title": "Лендинг <WordPress>", "score": 83,
                                               "requirements": ["Форма", "Адаптив"], "budget": "30 000",
                                               "currency": "₽", "deadline": "1 неделя",
                                               "score_reason": "Есть макет & понятный объём"})
    repo.set_job_details(item.id, "one_time_project", {"status": "direct_free", "method": "Telegram",
                                                     "url": "https://t.me/customer"})
    return item


def card_service(repo):
    return SimpleNamespace(repo=repo, owner_id=1, ai=SimpleNamespace(ask=AsyncMock(side_effect=lambda kind, text:
        {"draft": "Здравствуйте! Уточните требования к форме."} if kind == "draft" else
        {"plan": ["Проверить макет", "Сверстать страницу"], "questions": ["Какие поля формы нужны?"]})))


def callback(job, data):
    return SimpleNamespace(data=data.format(id=job.id), from_user=SimpleNamespace(id=1), answer=AsyncMock(),
                           message=SimpleNamespace(text="", edit_text=AsyncMock(), answer=AsyncMock()))


@pytest.mark.asyncio
async def test_notification_uses_full_card_once_and_keeps_delivery_guard(repo, job):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, SimpleNamespace(), SimpleNamespace(), bot, 1)
    await service.notify_job(job.id, "instant")
    await service.notify_job(job.id, "instant")
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args
    assert sent.kwargs["parse_mode"] == "HTML"
    assert "30 000 ₽" in sent.args[1] and "1 неделя" in sent.args[1] and "83/100" in sent.args[1]
    assert "&lt;WordPress&gt;" in sent.args[1]
    rows = sent.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in rows] == [1, 2, 2, 2]
    assert rows[2][0].url == "https://t.me/card_jobs/55"


@pytest.mark.asyncio
async def test_following_survives_status_change_restart_and_can_be_stopped(repo, job):
    service = card_service(repo)
    cards = OrderCards(service)
    tap = callback(job, "card:follow:{id}")
    await cards.handle(tap)
    await cards.handle(tap)  # A stale repeated tap is not a toggle.
    assert repo.job(job.id).saved
    tap.data = f"feedback:applied:{job.id}"
    await cards.handle(tap)
    assert repo.job(job.id).status == "applied" and repo.job(job.id).saved
    assert "Откликнулся" in tap.message.edit_text.await_args.args[0]
    reopened_repo = Repository(repo.factory)
    assert reopened_repo.followed_jobs()[0].id == job.id
    tap.data = f"card:unfollow:{job.id}"
    await OrderCards(SimpleNamespace(repo=reopened_repo)).handle(tap)
    assert not repo.job(job.id).saved and repo.job(job.id).status == "applied"
    assert not repo.followed_jobs()


@pytest.mark.asyncio
async def test_reject_removes_following_and_can_restore(repo, job):
    cards = OrderCards(card_service(repo))
    repo.set_job_following(job.id, True)
    tap = callback(job, "feedback:irrelevant:{id}")
    await cards.handle(tap)
    assert repo.job(job.id).status == "irrelevant" and not repo.job(job.id).saved
    assert "Вернуть заказ" in str(tap.message.edit_text.await_args.kwargs["reply_markup"])
    tap.data = f"card:follow:{job.id}"
    await cards.handle(tap)
    assert repo.job(job.id).status == "new" and repo.job(job.id).saved


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("note", "Согласовать сроки <без аврала>"), ("amount", "35 000 ₽")])
async def test_input_updates_same_message_and_cancel_does_not_save(repo, job, field, value):
    cards = OrderCards(card_service(repo))
    tap = callback(job, "field:" + field + ":{id}")
    await cards.handle(tap)
    assert "следующим сообщением" in tap.message.edit_text.await_args.args[0]
    user = SimpleNamespace(from_user=SimpleNamespace(id=1), text=value, answer=AsyncMock())
    assert await cards.accept_input(user)
    assert getattr(repo.job(job.id), field) == value
    assert "✓ Изменения сохранены" in tap.message.edit_text.await_args.args[0]
    tap.message.answer.assert_not_awaited()
    await cards.handle(tap)
    tap.data = f"card:cancel:{job.id}"
    await cards.handle(tap)
    assert not await cards.accept_input(user)
    assert getattr(repo.job(job.id), field) == value


@pytest.mark.asyncio
async def test_draft_generation_cached_owner_edit_reused_and_plan_stays_in_card(repo, job):
    service = card_service(repo)
    cards = OrderCards(service)
    tap = callback(job, "draft:{id}")
    await cards.handle(tap)
    await cards.handle(tap)
    service.ai.ask.assert_awaited_once()
    tap.data = f"field:draft:{job.id}"
    await cards.handle(tap)
    user = SimpleNamespace(from_user=SimpleNamespace(id=1), text="Мой исправленный отклик", answer=AsyncMock())
    assert await cards.accept_input(user)
    tap.data = f"draft:{job.id}"
    await OrderCards(service).handle(tap)
    assert "Мой исправленный отклик" in tap.message.edit_text.await_args.args[0]
    assert service.ai.ask.await_count == 1
    tap.data = f"plan:{job.id}"
    await cards.handle(tap)
    assert "Проверить макет" in tap.message.edit_text.await_args.args[0]
    assert "Какие поля формы" in tap.message.edit_text.await_args.args[0]
    tap.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_error_keeps_other_buttons_and_retry_available(repo, job):
    service = card_service(repo)
    service.ai.ask.side_effect = RuntimeError("API unavailable")
    tap = callback(job, "draft:{id}")
    cards = OrderCards(service)
    await cards.handle(tap)
    assert "Не удалось получить ответ ИИ" in tap.message.edit_text.await_args.args[0]
    assert "Отслеживать заказ" in str(tap.message.edit_text.await_args.kwargs["reply_markup"])
    assert not cards.generating and repo.analysis(job.id, "draft_saved") is None


@pytest.mark.asyncio
async def test_overlong_input_is_not_saved_and_old_callback_can_be_repeated(repo, job):
    cards = OrderCards(card_service(repo))
    tap = callback(job, "field:note:{id}")
    await cards.handle(tap)
    user = SimpleNamespace(from_user=SimpleNamespace(id=1), text="x" * 1001, answer=AsyncMock())
    assert await cards.accept_input(user)
    assert not repo.job(job.id).note and 1 in cards.pending
    tap.message.edit_text.side_effect = TelegramBadRequest(method="editMessageText", message="message is not modified")
    await cards.show(tap.message, job.id)


def test_html_is_valid_and_bounded_even_with_large_analysis(repo, job):
    repo.save_analysis(job.id, "draft_saved", "owner", {"draft": "<script>&" * 2000})
    text = card_text(repo, job.id, "draft", sample=True)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "🧪" in text and "не новый заказ" in text
    assert len(unescape(re.sub(r"<[^>]+>", "", text))) < 4096


@pytest.mark.parametrize("budget,currency", [("30 000 ₽", "₽"), ("30 000 ₽", "RUB"), ("30 000 руб", "₽")])
def test_budget_does_not_duplicate_currency(repo, job, budget, currency):
    data = dict(repo.analysis(job.id).result, budget=budget, currency=currency)
    repo.save_analysis(job.id, "deep", "test", data)
    assert f"<b>{budget}</b>" in card_text(repo, job.id)


@pytest.mark.asyncio
async def test_followed_list_opens_card_via_existing_router(repo, job):
    repo.set_job_following(job.id, True)
    router = create_router(card_service(repo))
    menu = next(item.callback for item in router.message.handlers if item.callback.__name__ == "menu_action")
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="👀 Отслеживаемые", answer=AsyncMock())
    await menu(message)
    assert message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"card:home:{job.id}"
    tap = callback(job, "card:home:{id}")
    await router.callback_query.handlers[0].callback(tap)
    assert "Лендинг &lt;WordPress&gt;" in tap.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_owner_middleware_blocks_card_input_from_other_user():
    handler = AsyncMock()
    event = SimpleNamespace(from_user=SimpleNamespace(id=99))
    await OwnerMiddleware(1)(handler, event, {})
    handler.assert_not_awaited()
