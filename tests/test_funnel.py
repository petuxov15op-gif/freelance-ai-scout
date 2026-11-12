from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from scout.core import contact_options, employment_type, high_risk_role, office_required
from scout.bot import create_router
from scout.service import ScoutService


def test_employment_and_contacts_from_text_and_entities():
    assert employment_type("Нужен сайт для кафе") == "one_time_project"
    assert employment_type("Регулярные задачи Python для агентства") == "recurring_project"
    assert employment_type("Подработка Python 3 часа в день") == "part_time_role"
    assert employment_type("Вакансия Python в штат") == "full_time_role"
    assert high_risk_role("Вакансия Senior DevOps инженера")
    assert not high_risk_role("Нужен Python-бот для кафе")
    assert office_required("Нужен разработчик, работа в офисе")
    assert not office_required("Нужен разработчик, удалённо")
    assert not office_required("Нужен разработчик, работа в офисе не требуется")
    assert contact_options("Пишите @client12345")["status"] == "direct_free"
    assert contact_options("Отклик: jobs@example.com")["method"] == "Email"
    assert contact_options("Нужен бот", [{"label": "Написать", "url": "https://t.me/client12345"}])["status"] == "direct_free"
    assert contact_options("Нужен бот", [{"label": "Откликнуться", "url": "https://forms.example.com/apply"}])["status"] == "unknown"
    assert contact_options("Бесплатный отклик: https://forms.example.com/apply")["status"] == "platform_free"
    assert contact_options("Нужен бот", [{"label": "Бесплатный отклик", "url": "https://forms.example.com/apply"}])["status"] == "platform_free"
    assert contact_options("Нужен бот", [{"label": "Пишите", "url": "mailto:jobs@example.com"}])["method"] == "Email"
    assert contact_options("Платный отклик: https://forms.example.com/apply")["status"] == "paid"
    assert contact_options("Пишите автору", author_username="author12345")["status"] == "direct_free"
    assert contact_options("Пишите автору")["status"] == "none"
    assert contact_options("Подробнее: https://example.com/article")["status"] == "none"
    assert contact_options("Пишите @channel123", source_username="channel123")["status"] == "none"
    assert contact_options("Пишите @SomeBot")["status"] == "none"


def test_distinct_customers_are_not_merged_by_template(repo):
    channel = repo.candidate(1, "jobs00001", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    now = datetime.now(timezone.utc)
    body = "Нужен сайт для компании, бюджет 25000 рублей, пишите заказчику"
    a = repo.save_message(channel.id, 1, body, "https://t.me/jobs00001/1", now,
                          {"contact_url": "https://t.me/customerA"})
    b = repo.save_message(channel.id, 2, body, "https://t.me/jobs00001/2", now,
                          {"contact_url": "https://t.me/customerB"})
    c = repo.save_message(channel.id, 3, body, "https://t.me/jobs00001/3", now,
                          {"contact_url": "https://t.me/customerA"})
    first, _ = repo.attach_job(a)
    second, created = repo.attach_job(b)
    repeated, repeated_created = repo.attach_job(c)
    assert created and first.id != second.id
    assert not repeated_created and repeated.id == first.id


def test_analytics_counts_unique_jobs_and_manual_feedback(repo):
    channel = repo.candidate(1, "jobs00001", "Jobs", "", "python bot", 70, "", [])
    repo.transition_channel(channel.id, "approved")
    now = datetime.now(timezone.utc)
    message = repo.save_message(channel.id, 1, "Нужен бот", None, now, {})
    job, _ = repo.attach_job(message)
    repo.save_analysis(job.id, "deep", "test", {"title": "Бот", "score": 80})
    repo.set_job_details(job.id, "part_time_role", {"status": "direct_free", "method": "Email", "url": "mailto:a@example.com"})
    repo.add_feedback(job.id, "applied")
    repo.add_feedback(job.id, "applied")
    repo.add_feedback(job.id, "replied")
    values = repo.analytics(None)
    assert values == {"found": 1, "jobs": 1, "unique": 1, "applied": 1, "replied": 1, "won": 0,
                      "replied_from_applied": 1, "won_from_applied": 0}
    assert repo.job(job.id).applied_at and repo.job(job.id).replied_at
    assert repo.top_channels() == [("jobs00001", 1, 1, 1, 0)]
    assert repo.top_queries(7) == [("python bot", 1)]


def test_alembic_upgrade_preserves_existing_ids_and_feedback(tmp_path):
    url = f"sqlite:///{tmp_path / 'existing.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO jobs (id, canonical_hash, status, created_at) VALUES (42, 'old-hash', 'applied', CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO feedback (id, job_id, action, created_at) VALUES (7, 42, 'applied', CURRENT_TIMESTAMP)"))
    command.upgrade(config, "head")
    with engine.connect() as db:
        assert db.execute(text("SELECT id, status, employment_type FROM jobs WHERE id=42")).one() == (42, "applied", "one_time_project")
        assert db.execute(text("SELECT applied_at FROM jobs WHERE id=42")).scalar() is not None
        assert db.execute(text("SELECT id, job_id FROM feedback WHERE id=7")).one() == (7, 42)
        assert db.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    engine.dispose()


@pytest.mark.asyncio
async def test_bot_settings_and_card_fields_are_saved(repo):
    channel = repo.candidate(1, "jobs00001", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    message = repo.save_message(channel.id, 1, "Нужен бот", None, datetime.now(timezone.utc), {})
    job, _ = repo.attach_job(message)
    service = ScoutService(repo, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), 1)
    router = create_router(service)
    callback_handler = router.callback_query.handlers[0].callback
    output = SimpleNamespace(edit_reply_markup=AsyncMock(), edit_media=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=1), message=output, answer=AsyncMock())

    callback.data = "toggle:employment:part_time_role"
    await callback_handler(callback)
    assert repo.setting("employment_part_time_role") == "false"

    callback.data = f"field:note:{job.id}"
    await callback_handler(callback)
    user_message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="Обсудить сроки",
                                   answer=AsyncMock())
    await router.message.handlers[-1].callback(user_message)
    assert repo.job(job.id).note == "Обсудить сроки"
