from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, User

from scout.bot import OwnerMiddleware, channel_intent, create_router
from scout.models import Message
from scout.repository import Repository
from scout.service import ScoutService


def add_channel(repo, telegram_id, title, username=None):
    channel = repo.candidate(telegram_id, username, title, "", None, 50, "", [])
    assert repo.transition_channel(channel.id, "approved", initial_cursor=4)
    return channel


def test_channel_status_and_history_survive_repository_reopen(repo):
    channel = add_channel(repo, 101, "Python Freelance", "python_freelance")
    old = repo.save_message(channel.id, 5, "Старый заказ", None, datetime.now(timezone.utc), {})
    assert repo.set_monitoring(channel.id, False)
    reopened = Repository(repo.factory)
    assert reopened.get_channel(channel.id).status == "paused"
    assert reopened.cursor(channel.id) == 5
    assert reopened.pending_messages() == []
    with repo.factory() as db:
        assert db.get(Message, old.id).text == "Старый заказ"
    assert reopened.set_monitoring(channel.id, True, latest_cursor=17)
    assert reopened.cursor(channel.id) == 17
    assert reopened.get_channel(channel.id).status == "approved"
    assert not reopened.set_monitoring(channel.id, True, latest_cursor=17)


def test_shared_job_uses_still_active_source(repo):
    first = add_channel(repo, 111, "First", "first_jobs")
    second = add_channel(repo, 112, "Second", "second_jobs")
    now = datetime.now(timezone.utc)
    text = "Нужен сайт для фирмы, пишите @customer"
    a = repo.save_message(first.id, 5, text, "https://t.me/first_jobs/5", now, {"contact_url": "https://t.me/customer"})
    b = repo.save_message(second.id, 6, text, "https://t.me/second_jobs/6", now, {"contact_url": "https://t.me/customer"})
    job, _ = repo.attach_job(a)
    same_job, _ = repo.attach_job(b)
    assert same_job.id == job.id
    assert repo.set_monitoring(first.id, False)
    assert repo.job_channel(job.id).id == second.id
    assert repo.job_link(job.id) == "https://t.me/second_jobs/6"


@pytest.mark.asyncio
async def test_disabled_channel_is_skipped_by_monitor_and_backfill(repo):
    first = add_channel(repo, 101, "First", "first_jobs")
    second = add_channel(repo, 102, "Second", "second_jobs")
    seen = []

    class Source:
        latest_message_id = AsyncMock(return_value=20)

        async def messages(self, channel, min_id):
            seen.append(("monitor", channel.id, min_id))
            if False:
                yield None

        async def messages_since(self, channel, since):
            seen.append(("backfill", channel.id))
            if False:
                yield None

    service = ScoutService(repo, Source(), SimpleNamespace(), SimpleNamespace(), 1)
    assert await service.set_channel_monitoring(first.id, False)
    await service.monitor()
    assert await service.backfill_today() == (1, 0, 0, [])
    assert seen == [("monitor", second.id, 4), ("backfill", second.id)]
    assert await service.set_channel_monitoring(first.id, True)
    assert repo.cursor(first.id) == 20
    await service.monitor()
    assert seen[-2:] == [("monitor", first.id, 20), ("monitor", second.id, 4)]


def test_russian_intents():
    assert channel_intent("Покажи мои каналы") == ("list", "all")
    assert channel_intent("Список источников") == ("list", "all")
    assert channel_intent("Покажи только активные каналы") == ("list", "approved")
    assert channel_intent("Покажи каналы, которые я отключил") == ("list", "paused")
    assert channel_intent("Больше не следи за Python Freelance") == ("off", "python freelance")
    assert channel_intent("Отключи второй канал") == ("off", "второй")
    assert channel_intent("Включи обратно IT Orders") == ("on", "it orders")
    assert channel_intent("Перестань мониторить @example_channel") == ("off", "@example_channel")


@pytest.mark.asyncio
async def test_page_filters_links_private_and_text_selection(repo):
    channels = [add_channel(repo, 100 + index, "Same" if index < 2 else f"Job {index}",
                            f"jobs_{index:05d}" if index != 2 else None) for index in range(7)]
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=30))
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    router = create_router(service)
    handlers = {item.callback.__name__: item.callback for item in router.message.handlers}
    callback_handler = router.callback_query.handlers[0].callback
    message = SimpleNamespace(text="/channels", from_user=SimpleNamespace(id=1), answer=AsyncMock(),
                              edit_text=AsyncMock())
    await handlers["sources"](message)
    text, = message.answer.await_args.args
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "Всего: 7" in text and "Страница 1/2" in text
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/jobs_00000"
    assert keyboard.inline_keyboard[2][0].callback_data == "my:unavailable"

    message.text = "Отключи Same"
    await handlers["channel_list"](message)
    choice = message.answer.await_args.kwargs["reply_markup"]
    assert len(choice.inline_keyboard) == 2
    assert all(row[0].callback_data.startswith("my:off:") for row in choice.inline_keyboard)
    assert repo.get_channel(channels[0].id).status == "approved"

    message.text = "Отключи второй канал"
    await handlers["channel_list"](message)
    assert repo.get_channel(channels[1].id).status == "paused"
    message.text = "Отключи Same"
    await handlers["channel_list"](message)
    assert repo.get_channel(channels[0].id).status == "paused"
    callback = SimpleNamespace(data="my:list:paused:1", from_user=SimpleNamespace(id=1),
                               message=message, answer=AsyncMock())
    await callback_handler(callback)
    assert "Отключённые" in message.edit_text.await_args.args[0]
    callback.data = "my:list:all:2"
    await callback_handler(callback)
    assert "Страница 2/2" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_toggle_button_updates_message_and_handles_repeat(repo):
    channel = add_channel(repo, 201, "Jobs", "jobs_public")
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=42))
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    callback_handler = create_router(service).callback_query.handlers[0].callback
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(data=f"my:off:{channel.id}:all:1", from_user=SimpleNamespace(id=1),
                               message=message, answer=AsyncMock())
    await callback_handler(callback)
    assert repo.get_channel(channel.id).status == "paused"
    assert "Мониторинг отключён" in message.edit_text.await_args.args[0]
    await callback_handler(callback)
    assert repo.get_channel(channel.id).status == "paused"
    assert "уже установлен" in callback.answer.await_args.args[0]
    callback.data = f"my:on:{channel.id}:all:1"
    await callback_handler(callback)
    assert repo.get_channel(channel.id).status == "approved"
    assert repo.cursor(channel.id) == 42
    source.latest_message_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_keeps_private_channel_paused_when_access_fails(repo):
    channel = add_channel(repo, 301, "Private")
    source = SimpleNamespace(latest_message_id=AsyncMock(side_effect=RuntimeError("unavailable")))
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    assert await service.set_channel_monitoring(channel.id, False)
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.set_channel_monitoring(channel.id, True)
    assert repo.get_channel(channel.id).status == "paused"
    assert repo.cursor(channel.id) == 4


@pytest.mark.asyncio
async def test_foreign_user_cannot_use_management_callback():
    middleware = OwnerMiddleware(1)
    event = CallbackQuery.model_construct(id="1", from_user=User.model_construct(id=2, is_bot=False, first_name="Other"),
                                          chat_instance="1", data="my:off:1:all:1")
    answer = AsyncMock()
    object.__setattr__(event, "answer", answer)
    handler = AsyncMock()
    await middleware(handler, event, {})
    handler.assert_not_awaited()
    answer.assert_awaited_once_with("Доступ запрещён", show_alert=True)
