from types import SimpleNamespace
from unittest.mock import AsyncMock

from scout.bot import create_router
from scout.demo_widget import STATE_KEY, handle_widget, widget_keyboard, widget_text


def test_sample_has_actual_callback_buttons_and_is_explicitly_fictional():
    buttons = [b for row in widget_keyboard().inline_keyboard for b in row]
    assert {b.callback_data for b in buttons} == {"demo:follow", "demo:ignore", "demo:details", "demo:check"}
    assert all(b.url is None for b in buttons)
    assert "Вымышленный пример" in widget_text()


async def test_follow_stop_ignore_and_reset_persist_without_creating_real_jobs(repo):
    callback = SimpleNamespace(data="demo:follow", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    for action, expected in [("follow", "following"), ("stop", "new"), ("ignore", "ignored"), ("reset", "new")]:
        callback.data = "demo:" + action
        await handle_widget(callback, repo)
        assert repo.setting(STATE_KEY) == expected
    assert repo.jobs() == []
    assert repo.monitored_channels() == []


async def test_details_and_check_preserve_selected_state(repo):
    repo.set_setting(STATE_KEY, "following")
    callback = SimpleNamespace(data="demo:details", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await handle_widget(callback, repo)
    assert "Что нужно уточнить" in callback.message.edit_text.await_args.args[0]
    assert repo.setting(STATE_KEY) == "following"
    callback.data = "demo:check"
    await handle_widget(callback, repo)
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "Отслеживается" in callback.answer.await_args.args[0]


async def test_demo_callbacks_are_wired_into_existing_bot_router(repo):
    router = create_router(SimpleNamespace(repo=repo, owner_id=42))
    callback = SimpleNamespace(data="demo:follow", message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await router.callback_query.handlers[0].callback(callback)
    assert repo.setting(STATE_KEY) == "following"
    assert "Остановить отслеживание" in str(callback.message.edit_text.await_args.kwargs["reply_markup"])
