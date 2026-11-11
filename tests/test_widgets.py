import asyncio
import json
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from aiogram import Bot
from alembic import command
from alembic.config import Config as AlembicConfig

from scout.service import ScoutService
from scout.widgets import (AnswerWidgetQuery, EditMessageWidgetData, SendWidget,
                           WidgetController, WidgetDispatcher, packed, widget_html)


def seeded(repo):
    channel = repo.candidate(101, "scoutjobs", "Заказы разработки", "IT-проекты", None, 75, "", [])
    repo.transition_channel(channel.id, "approved")
    message = repo.save_message(channel.id, 1, "Нужен сайт для небольшой компании", "https://t.me/scoutjobs/1",
                                datetime.now(timezone.utc), {})
    job, _ = repo.attach_job(message)
    repo.save_analysis(job.id, "deep", "test", {"title": "Разработка сайта", "score": 85,
                                                   "category": "Веб", "budget": "50000", "currency": "₽",
                                                   "tools": ["Python"], "requirements": ["Сайт"],
                                                   "score_reason": "Подходит"})
    repo.set_job_details(job.id, "one_time_project", {"status": "direct_free", "method": "Telegram", "url": None})
    return channel, job


def controller(repo):
    bot = AsyncMock()
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=5))
    service = ScoutService(repo, source, SimpleNamespace(ask=AsyncMock(return_value={"draft": "Здравствуйте!"})), bot, 1)
    return WidgetController(service), bot


def query(data, user_id=1, message_id=40):
    return {"id": "q1", "from": {"id": user_id}, "chat": {"id": 1, "type": "private"},
            "message_id": message_id, "data": data}


def test_widget_document_and_state_limits(repo):
    channel, job = seeded(repo)
    widget, _ = controller(repo)
    html = widget_html().encode("utf-8")
    assert len(html) <= 32768
    assert len(zlib.compress(html, 9)) <= 4096
    job_html = widget_html("j").encode("utf-8")
    assert len(job_html) <= 32768
    assert len(zlib.compress(job_html, 9)) <= 4096
    assert b"#eaf4ff" in job_html
    assert b'data-a="j:' in job_html
    assert "fetch(" not in widget_html()
    assert "prefers-reduced-motion" in widget_html()
    assert packed(widget.job(job.id))
    assert packed(widget.dashboard())
    assert packed(widget.sources())
    assert widget.job(job.id)["score"] == 85
    assert widget.dashboard()["m"] == 1


def test_widget_methods_serialize_for_existing_aiogram_session():
    bot = Bot("123456:TESTTOKEN")
    methods = [SendWidget(chat_id=1, html="<b>x</b>", width=340, height=500, chat_data="{}"),
               AnswerWidgetQuery(widget_query_id="q", data="{}"),
               EditMessageWidgetData(chat_id=1, message_id=2, chat_data="{}")]
    for method in methods:
        names = {field[0]["name"] for field in bot.session.build_form_data(bot, method)._fields}
        assert "chat_data" in names or "data" in names
    assert methods[0].__api_method__ == "sendWidget"


def test_removed_source_can_be_submitted_again_without_erasing_history(repo):
    channel, job = seeded(repo)
    assert repo.remove_source(channel.id)
    restored = repo.candidate(101, "scoutjobs", "Заказы разработки", "IT-проекты", None, 75, "", [])
    assert restored.status == "pending"
    assert repo.transition_channel(restored.id, "approved", initial_cursor=5)
    assert repo.job(job.id) is not None
    assert repo.cursor(channel.id) == 5


def test_widget_migration_keeps_existing_job_decisions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "old.db"
    config = AlembicConfig(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "0002")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO jobs (id,canonical_hash,status,created_at) VALUES (1,'a','interesting','2026-11-11')")
        db.execute("INSERT INTO jobs (id,canonical_hash,status,created_at) VALUES (2,'b','irrelevant','2026-11-11')")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT id, status, saved, hidden FROM jobs ORDER BY id").fetchall() == [
            (1, "interesting", 1, 0), (2, "irrelevant", 0, 1)]


@pytest.mark.asyncio
async def test_widget_auth_save_hide_and_real_db(repo):
    _, job = seeded(repo)
    widget, bot = controller(repo)
    await widget.handle(query(f"j:s:{job.id}", user_id=999))
    assert repo.job(job.id).status == "new"
    assert repo.job(job.id).saved is False
    assert isinstance(bot.call_args.args[0], AnswerWidgetQuery)
    await widget.handle(query(f"j:s:{job.id}"))
    assert repo.job(job.id).saved is True
    assert repo.job(job.id).status == "new"
    assert widget.dashboard()["s"] == 1
    assert any(isinstance(c.args[0], EditMessageWidgetData) for c in bot.call_args_list)
    await widget.handle(query(f"j:s:{job.id}"))
    assert repo.job(job.id).saved is True
    await widget.handle(query(f"j:h:{job.id}"))
    assert repo.job(job.id).hidden is True
    assert repo.widget_jobs() == []
    assert repo.job(job.id).status == "new"
    await widget.handle(query(f"j:u:{job.id}"))
    assert repo.job(job.id).hidden is False
    assert len(repo.widget_jobs()) == 1


@pytest.mark.asyncio
async def test_widget_flags_preserve_application_status(repo):
    _, job = seeded(repo)
    widget, _ = controller(repo)
    repo.add_feedback(job.id, "applied")
    await widget.handle(query(f"j:s:{job.id}"))
    await widget.handle(query(f"j:h:{job.id}"))
    assert repo.job(job.id).status == "applied"
    assert repo.job(job.id).applied_at is not None
    assert repo.job(job.id).saved and repo.job(job.id).hidden


@pytest.mark.asyncio
async def test_widget_source_toggle_and_confirmed_remove_preserve_history(repo):
    channel, job = seeded(repo)
    widget, bot = controller(repo)
    await widget.handle(query(f"s:t:{channel.id}:1"))
    assert repo.get_channel(channel.id).status == "paused"
    assert repo.job(job.id) is not None
    await widget.handle(query(f"s:t:{channel.id}:1"))
    assert repo.get_channel(channel.id).status == "approved"
    widget.service.source.latest_message_id.assert_awaited_once()
    await widget.handle(query(f"s:t:{channel.id}:1"))
    assert repo.get_channel(channel.id).status == "paused"
    await widget.handle(query(f"s:y:{channel.id}:1"))
    assert repo.get_channel(channel.id).status == "paused"
    await widget.handle(query(f"s:x:{channel.id}:1"))
    assert json.loads(bot.call_args.args[0].data)["confirm"] == channel.id
    await widget.handle(query(f"s:y:{channel.id}:1"))
    assert repo.get_channel(channel.id).status == "removed"
    assert repo.job(job.id) is not None
    assert repo.job_original(job.id)
    assert not repo.monitored_channels()


@pytest.mark.asyncio
async def test_widget_dispatcher_handles_new_update_without_second_poller(repo):
    seen = AsyncMock()
    dispatcher = WidgetDispatcher(seen)
    update = Update.model_validate({"update_id": 1, "widget_query": query("d:r")})
    await dispatcher._process_update(AsyncMock(), update)
    seen.assert_awaited_once_with(query("d:r"))


@pytest.mark.asyncio
async def test_widget_send_uses_bot_api_and_fallback(repo):
    _, job = seeded(repo)
    widget, bot = controller(repo)
    await widget.send("d")
    method = bot.call_args.args[0]
    assert isinstance(method, SendWidget)
    assert method.allow_queries is True
    await widget.send("j", job.id)
    method = bot.call_args.args[0]
    assert method.height == 580
    assert method.html == widget_html("j")
    bot.side_effect = RuntimeError("unsupported")
    await widget.send("s")
    bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_instant_notification_keeps_text_and_adds_widget(repo):
    _, job = seeded(repo)
    widget, bot = controller(repo)
    bot.send_message.return_value = SimpleNamespace(message_id=42)
    widget.service.widgets = SimpleNamespace(send=AsyncMock())
    await widget.service.notify_job(job.id, "instant")
    bot.send_message.assert_awaited_once()
    widget.service.widgets.send.assert_awaited_once_with("j", job.id, fallback=False)
