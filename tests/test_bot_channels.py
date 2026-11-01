import pytest
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scout.bot import (MENU_LABELS, MENU_SECTIONS, bot_commands, candidate_digest_keyboard,
                       create_router, health_text, main_menu, parse_channel_list, section_menu)
from scout.service import ScoutService


def test_channel_list_accepts_handles_and_links():
    assert parse_channel_list("@job_developer, @FrWork3 https://t.me/digitaltender @job_developer") == [
        "job_developer", "FrWork3", "digitaltender"
    ]


def test_channel_list_rejects_other_text():
    with pytest.raises(ValueError):
        parse_channel_list("@job_developer hello")


def test_main_menu_groups_every_action_and_telegram_command_menu():
    labels = [button.text for row in main_menu().keyboard for button in row]
    assert labels == ["📋 Заказы", "📡 Каналы", "➕ Добавить канал", "📊 Бот"]
    assert {action for actions in MENU_SECTIONS.values() for action in actions} == set(MENU_LABELS)
    assert [[button.text for button in row] for row in section_menu("📡 Каналы").keyboard] == [
        ["📡 Источники", "🕓 Кандидаты"], ["🔎 Найти чаты сейчас"], ["← Назад"]
    ]
    assert main_menu().is_persistent is True
    assert {command.command for command in bot_commands()} >= {"menu", "sources", "add", "settings"}
    assert "discover" not in {command.command for command in bot_commands()}
    assert "➕ Добавить канал" in labels
    assert "🔎 Найти чаты сейчас" in MENU_LABELS.values()
    assert "🩺 Проверить" in MENU_LABELS.values()
    assert "check" in {command.command for command in bot_commands()}
    assert all("Поиск каналов" not in label for label in labels)


@pytest.mark.asyncio
async def test_manual_search_menu_button_starts_discovery_and_reports_result(repo):
    service = SimpleNamespace(owner_id=1, repo=repo, discovery_lock=asyncio.Lock(),
                              discover=AsyncMock(return_value=("done", 2, 0, None)))
    router = create_router(service)
    handler = next(item.callback for item in router.message.handlers if item.callback.__name__ == "menu_action")
    message = SimpleNamespace(text="🔎 Найти чаты сейчас", answer=AsyncMock())

    await handler(message)

    service.discover.assert_awaited_once_with()
    assert "Запускаю поиск" in message.answer.await_args_list[0].args[0]
    assert "Новых чатов на рассмотрение: 2" in message.answer.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_menu_navigation_switches_reply_keyboard(repo):
    service = SimpleNamespace(owner_id=1, repo=repo)
    handler = next(item.callback for item in create_router(service).message.handlers
                   if item.callback.__name__ == "menu_action")
    message = SimpleNamespace(text="📋 Заказы", answer=AsyncMock())

    await handler(message)
    submenu = message.answer.await_args.kwargs["reply_markup"]
    assert [[button.text for button in row] for row in submenu.keyboard] == [
        ["📋 Последние", "⭐ Лучшие"], ["← Назад"]]

    message.text = "← Назад"
    await handler(message)
    assert message.answer.await_args.kwargs["reply_markup"] == main_menu()


@pytest.mark.asyncio
async def test_digest_decision_removes_only_its_buttons(repo):
    first = repo.candidate(101, "jobs00001", "First", "", "query", 50, "IT", ["Нужен сайт"])
    second = repo.candidate(102, "jobs00002", "Second", "", "query", 50, "IT", ["Нужен бот"])
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=9))
    service = SimpleNamespace(owner_id=1, repo=repo, source=source)
    callback_handler = create_router(service).callback_query.handlers[0].callback
    message = SimpleNamespace(text="Новые чаты на рассмотрение:",
                              reply_markup=candidate_digest_keyboard([first.id, second.id]),
                              edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(data=f"channel:approved:{first.id}", message=message, answer=AsyncMock())

    await callback_handler(callback)

    assert repo.get_channel(first.id).status == "approved"
    markup = message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].callback_data == f"channel:approved:{second.id}"


@pytest.mark.asyncio
async def test_health_report_shows_monitor_error_and_ai_failure(repo):
    channel = repo.candidate(100, "jobs", "Jobs", "", None, 50, "", [])
    repo.transition_channel(channel.id, "approved")
    source = SimpleNamespace(client=SimpleNamespace(is_connected=lambda: True))
    ai = SimpleNamespace(check_health=AsyncMock(return_value=(False, "Недостаточно средств (HTTP 402).")))
    service = ScoutService(repo, source, ai, SimpleNamespace(), 1)
    service.last_monitor_started = datetime.now(timezone.utc)
    service.last_monitor_errors = ["@jobs: RuntimeError"]

    report = await health_text(service)

    assert "База данных: работает" in report
    assert "За 24 часа:" in report
    assert "Недостаточно средств" in report
    assert "@jobs: RuntimeError" in report
    assert "Итог: обнаружены проблемы" in report
