from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types
from telethon.errors import FloodWaitError

from scout.ai import DRAFT_VERSION
from scout.bot import create_router, job_keyboard
from scout.core import contact_options
from scout.models import Channel, Message, MonitoringState
from scout.outreach import outreach_username
from scout.portfolio import CASES
from scout.service import DISCOVERY_GROUPS, ScoutService, discovery_fit
from scout.telegram import chat_search_term


def test_audited_sources_pause_only_confirmed_intermediaries(repo):
    service = ScoutService(repo, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), 1)
    for username in ("remoteFreelanceJob", "workmasterIT", "design_ai_jobs", "zakazy_it",
                     "jobsCode", "mobileDevJobs", "development_vakans"):
        assert service.source_paused(SimpleNamespace(username=username, title="Jobs", description=""))
    for username in ("sova_freelance", "workk_on", "webjobagregator"):
        assert not service.source_paused(SimpleNamespace(username=username, title="Jobs", description=""))


@pytest.mark.parametrize("announcement, expected", [
    ("Нужен лендинг для кафе, разовый заказ, бюджет 15000 рублей", True),
    ("Нужен дизайн сайта и логотипа, бюджет 15000 рублей", False),
    ("Ищем Python-разработчика в штат на полный день", False),
    ("Подработка: поддерживать Python-бота по 3 часа в день", False),
    ("Подработка: делать веб-приложение с Codex по 3 часа в день", True),
    ("Нужен дизайнер интерфейса на полный день, вакансия", False),
])
def test_discovery_fit_prefers_freelance_and_vibe_roles(announcement, expected):
    assert discovery_fit(announcement) is expected


def test_discovery_queries_focus_on_freelance_and_vibe_coding():
    phrases = {phrase for group in DISCOVERY_GROUPS.values() for phrase in group}
    assert "ищу программиста" not in phrases
    assert "part time developer" not in phrases
    assert "заказ на лендинг" in phrases
    assert "вайбкодинг подработка" in phrases


@pytest.mark.asyncio
async def test_enabling_platforms_starts_at_new_posts(repo):
    channel = repo.candidate(100, "workmasterIT", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved", initial_cursor=5)
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=20))
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    await service.set_platforms_enabled(True)
    assert repo.cursor(channel.id) == 20
    assert service.platforms_enabled()
    await service.set_platforms_enabled(True)
    source.latest_message_id.assert_awaited_once()


class FakeSource:
    async def messages(self, channel, min_id):
        if min_id < 1:
            yield SimpleNamespace(id=1, raw_text="Нужен сайт для небольшой компании, оплата и детали по договоренности. Пишите заказчику @client12345",
                                  date=datetime.now(timezone.utc), media=None, fwd_from=None)

    async def resolve(self, username):
        return SimpleNamespace(id=100, username=username, title="Jobs", broadcast=True)

    async def inspect(self, entity):
        raise RuntimeError("История временно недоступна")

    async def description(self, entity):
        return "Публичный канал"


class FakeAI:
    model = "test-model"

    def __init__(self, fail=False):
        self.fail = fail

    async def ask(self, kind, text):
        if self.fail:
            raise RuntimeError("OpenRouter недоступен")
        if kind in {"prefilter", "kwork_prefilter"}:
            return {"is_job": True, "is_it": True, "codex_feasible": True,
                    "is_ongoing": False, "ai_coding_explicit": False, "reason": "заказ"}
        return {"title": "Сайт", "category": "сайт", "requirements": ["страница"],
                "budget": None, "currency": None, "deadline": None,
                "score_reason": "выполнимо", "score": 85}


@pytest.mark.asyncio
async def test_digest_keeps_individual_orders_and_contacts_on_retry(repo):
    channel = repo.candidate(100, "digest_jobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    first = "\u200b#вебдизайнер\n🔥 Нужно сделать сайт для мастерской.\n➡️ +70000000001"
    second = "#вебдизайнер\nНужно создать одностраничный сайт для тренера.\nНаписать"
    unrelated = "#иллюстратор\nНужен Senior иллюстратор и сайт для агентства.\nНаписать"
    text = "\n\n".join((first, second, unrelated))
    link_offset = len(text[:text.index("Написать")].encode("utf-16-le")) // 2
    unrelated_offset = len(text[:text.rindex("Написать")].encode("utf-16-le")) // 2
    item = SimpleNamespace(id=42, raw_text=text, date=datetime.now(timezone.utc), media=None, fwd_from=None,
                           entities=[types.MessageEntityTextUrl(link_offset, 8, "https://t.me/realclient"),
                                     types.MessageEntityTextUrl(unrelated_offset, 8, "https://t.me/otherclient")])
    ai = FakeAI()
    ai.ask = AsyncMock(side_effect=ai.ask)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, SimpleNamespace(), ai, bot, 1)
    service._save_item(channel, item)
    await service.process_pending()
    service._save_item(channel, item)
    await service.process_pending()

    assert repo.cursor(channel.id) == 42
    assert repo.stats()["messages"] == 1
    assert {job.contact_url for job in repo.jobs()} == {"tel:+70000000001", "https://t.me/realclient"}
    assert {repo.job_original(job.id) for job in repo.jobs()} == {first, second}
    assert {repo.job_link(job.id) for job in repo.jobs()} == {"https://t.me/digest_jobs/42"}
    assert ai.ask.await_count == 4
    assert bot.send_message.await_count == 2
    with repo.factory() as db:
        assert db.query(Message).filter_by(rejection_reason="requires_verified_expertise", part_number=3).count() == 1


@pytest.mark.asyncio
async def test_borderline_kwork_project_must_pass_ai_prefilter(repo):
    channel = repo.candidate(100, "kwork_jobs", "Kwork", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    text = "Доработка сервис деска на базе GLPI. Требуется настройка, интегарции и создание правил. Бюджет 10 000 рублей."
    repo.set_setting("platform_sources_enabled", "true")
    ai = FakeAI()
    ai.ask = AsyncMock(return_value={"is_job": True, "is_it": True, "codex_feasible": False})
    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, SimpleNamespace(), ai, bot, 1)
    found = repo.save_message(channel.id, 41, text, "https://kwork.ru/projects/1234/view", datetime.now(timezone.utc),
                              {"outbound_links": [{"url": "https://kwork.ru/projects/1234/view"}]})
    await service.process_pending()
    ai.ask.assert_awaited_once_with("kwork_prefilter", text)
    bot.send_message.assert_not_awaited()
    with repo.factory() as db:
        assert db.get(Message, found.id).rejection_reason == "ai_not_feasible"


@pytest.mark.asyncio
async def test_feedback_marks_card_and_allows_changing_decision(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, FakeSource(), FakeAI(), bot, 1)
    await service.monitor()
    job = repo.jobs()[0]
    callback_handler = create_router(service).callback_query.handlers[0].callback
    message = SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock(), from_user=SimpleNamespace(id=1))

    assert "Не подходит" in str(job_keyboard(job.id, None))
    callback.data = f"feedback:irrelevant:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "irrelevant"
    assert "❌ Не подходит" in message.edit_text.await_args.args[0]
    assert "Вернуть заказ" in str(message.edit_text.await_args.kwargs["reply_markup"])
    assert "📌 Статус" in str(message.edit_text.await_args.kwargs["reply_markup"])

    callback.data = f"feedback:change:{job.id}"
    await callback_handler(callback)
    assert "Снять статус" in str(message.edit_text.await_args.kwargs["reply_markup"])

    callback.data = f"feedback:interesting:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "interesting"
    assert "⭐ Интересно" in message.edit_text.await_args.args[0]

    callback.data = f"feedback:new:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "new"
    assert "🆕 Новый" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_monitor_recovers_after_ai_outage(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, FakeSource(), FakeAI(fail=True), bot, 1)
    await service.monitor()
    assert repo.cursor(channel.id) == 1
    assert len(repo.pending_messages()) == 1
    assert service.last_monitor_started is not None
    assert service.last_monitor_errors == ["Обработка объявления: RuntimeError"]
    service.ai = FakeAI()
    await service.monitor()
    assert len(repo.pending_messages()) == 0
    assert service.last_monitor_errors == []
    assert repo.stats()["jobs"] == 1
    assert bot.send_message.await_count == 1
    await service.monitor()
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_due_monitor_favors_applied_channel_and_slows_quiet_one(repo):
    good = repo.candidate(100, "good_jobs", "Good", "", None, 70, "", [])
    normal = repo.candidate(200, "normal_jobs", "Normal", "", None, 70, "", [])
    quiet = repo.candidate(300, "quiet_jobs", "Quiet", "", None, 70, "", [])
    for channel in (good, normal, quiet):
        repo.transition_channel(channel.id, "approved")
    now = datetime.now(timezone.utc)
    message = repo.save_message(good.id, 1, "Нужен сайт, пишите @customer123", None, now, {})
    job, _ = repo.attach_job(message)
    repo.mark_message(message.id, "linked")
    repo.add_feedback(job.id, "applied")
    with repo.factory() as db:
        db.get(Channel, quiet.id).reviewed_at = now - timedelta(days=8)
        db.commit()

    class Source:
        def __init__(self):
            self.seen = []

        async def messages(self, channel, min_id):
            self.seen.append(channel.username)
            if False:
                yield None

    source = Source()
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    await service.monitor()
    source.seen.clear()
    with repo.factory() as db:
        for channel in (good, normal, quiet):
            db.get(MonitoringState, channel.id).checked_at = now - timedelta(minutes=3)
        db.commit()
    intervals = {row["channel"].id: service.channel_interval(row) for row in repo.channel_report()}
    assert [intervals[channel.id] for channel in (good, normal, quiet)] == [2, 10, 30]
    await service.monitor_due()
    assert source.seen == ["good_jobs"]
    source.seen.clear()
    with repo.factory() as db:
        db.get(MonitoringState, normal.id).checked_at = now - timedelta(minutes=11)
        db.get(MonitoringState, quiet.id).checked_at = now - timedelta(minutes=31)
        db.commit()
    await service.monitor_due()
    assert source.seen == ["normal_jobs", "quiet_jobs"]


@pytest.mark.asyncio
async def test_channel_check_time_records_only_completed_scan(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class Source:
        failed = True

        async def messages(self, channel, min_id):
            if self.failed:
                raise ConnectionError("read interrupted")
            if False:
                yield None

    source = Source()
    service = ScoutService(repo, source, SimpleNamespace(), SimpleNamespace(), 1)
    await service.monitor()
    with repo.factory() as db:
        assert db.get(MonitoringState, channel.id).checked_at is None
    source.failed = False
    await service.monitor_due()
    with repo.factory() as db:
        assert db.get(MonitoringState, channel.id).checked_at is not None


@pytest.mark.asyncio
async def test_backfill_today_checks_posts_before_cursor_without_duplicate_notifications(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved", initial_cursor=10)
    post = SimpleNamespace(id=12, raw_text="Нужен сайт для небольшой компании, оплата и детали по договоренности. Пишите заказчику @client12345",
                           date=datetime.now(timezone.utc), media=None, fwd_from=None, entities=None, buttons=None)

    class Source(FakeSource):
        async def messages_since(self, channel, since):
            assert since <= post.date
            yield post

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, Source(), FakeAI(), bot, 1)
    assert await service.backfill_today() == (1, 1, 1, 1, [])
    assert repo.cursor(channel.id) == 10
    assert repo.stats()["jobs"] == 1
    assert await service.backfill_today() == (1, 1, 0, 0, [])
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_suitable_job_is_sent_without_score_threshold(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class SuitableAI(FakeAI):
        async def ask(self, kind, text):
            result = await super().ask(kind, text)
            if kind == "deep":
                result["score"] = 25
            return result

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, FakeSource(), SuitableAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_awaited_once()
    await service.monitor()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_priority_direct_site_requests_skip_slow_prefilter_and_warn_on_synthetic_contacts(repo):
    channel = repo.candidate(100, "program_job", "IT заказы", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class Source(FakeSource):
        async def messages(self, channel, min_id):
            posts = (
                "Нужен создатель сайтов на задачу за 80 т.р., кому интересно пишите в лс скину где заказ\nКонтакты: @kara_greer_43",
                "Нужен создатель сайтов на Тильде для переноса и доработки лендинга с Figma бюджет 80 т.р. Нужно собрать страницу по макету, настроить анимации и формы, адаптив под мобильные. Срок около недели. По всем вопросам к @tamsin_ramsey_82\nКонтакты: @charlotte_atherley_95",
                "Нужен верстальщик на сайт бюджет 30 т.р. пишите в лс скину где заказ\nКонтакты: @taylor_justin_43",
                "Ищу человека кто сможет сделать сайт на Тильде и затем дать доступ для дальнейших поправок\nКонтакты: @client55555",
            )
            for number, body in enumerate(posts, 1):
                yield SimpleNamespace(id=number, raw_text=body, date=datetime.now(timezone.utc),
                                      media=None, fwd_from=None)

    class RecordingAI(FakeAI):
        def __init__(self):
            super().__init__()
            self.prefilter_calls = 0

        async def ask(self, kind, text):
            if kind == "prefilter":
                self.prefilter_calls += 1
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    ai = RecordingAI()
    service = ScoutService(repo, Source(), ai, bot, 1)
    await service.monitor()
    assert ai.prefilter_calls == 0
    assert repo.stats()["jobs"] == 4
    assert bot.send_message.await_count == 4
    assert all(job.contact_status == "direct_free" for job in repo.jobs())
    assert all("⚠️ Контакт похож" in call.args[1] for call in bot.send_message.await_args_list[:3])
    assert "⚠️ Контакт похож" not in bot.send_message.await_args_list[3].args[1]
    assert [(item.status, item.rejection_reason) for item in repo.pending_messages()] == []
    await service.monitor()
    assert bot.send_message.await_count == 4


@pytest.mark.asyncio
async def test_priority_channel_is_processed_first_and_new_post_gets_ready_reply(repo):
    other = repo.candidate(100, "other_jobs", "Other", "", None, 70, "", [])
    priority = repo.candidate(200, "program_job", "Priority", "", None, 70, "", [])
    repo.transition_channel(other.id, "approved")
    repo.transition_channel(priority.id, "approved")
    reply = "Здравствуйте! Сделаю сайт на Тильде и передам доступ. Пришлите, пожалуйста, описание страниц для оценки сроков и стоимости."

    class Source(FakeSource):
        def __init__(self):
            self.seen = []
            self.new_post = False

        async def messages(self, channel, min_id):
            self.seen.append(channel.username)
            if min_id < 1:
                body = ("Ищу человека кто сможет сделать сайт на Тильде. Контакты: @client55555"
                        if channel.username == "program_job" else
                        "Нужен Python бот для заявок. Пишите @other55555")
                yield SimpleNamespace(id=1, raw_text=body, date=datetime.now(timezone.utc),
                                      media=None, fwd_from=None)
            if self.new_post and channel.username == "program_job" and min_id < 2:
                yield SimpleNamespace(id=2, raw_text="Нужен сайт для кафе. Пишите @cafe55555",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    class ReplyAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "draft":
                return {"draft": reply, "case_id": None, "draft_version": DRAFT_VERSION}
            return await super().ask(kind, text)

    source = Source()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=(SimpleNamespace(message_id=42),
                                                              SimpleNamespace(message_id=43),
                                                              SimpleNamespace(message_id=44))))
    service = ScoutService(repo, source, ReplyAI(), bot, 1)
    await service.monitor()
    assert source.seen[:2] == ["program_job", "other_jobs"]
    first = bot.send_message.await_args_list[0]
    assert first.args[0] == 1 and "Готовый отклик" in first.args[1] and reply in first.args[1]
    assert first.kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text == reply
    source.seen.clear()
    source.new_post = True
    await service.monitor_priority()
    assert source.seen == ["program_job"]
    assert bot.send_message.await_count == 3
    await service.monitor_priority()
    assert bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_priority_order_has_fallback_reply_when_generation_fails(repo):
    channel = repo.candidate(200, "program_job", "Priority", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class FailingDraftAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "draft":
                raise RuntimeError("Draft unavailable")
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, FakeSource(), FailingDraftAI(), bot, 1)
    await service.monitor_priority()
    bot.send_message.assert_awaited_once()
    assert "Готовый отклик" in bot.send_message.await_args.args[1]
    assert "материалы по задаче" in bot.send_message.await_args.args[1]


@pytest.mark.parametrize(("body", "links", "expected"), [
    ("Пишите @client12345", [], "client12345"),
    ("Контакты: @client12345 и @other54321", [], None),
    ("Контакты: @first_last_12", [], None),
    ("Платный отклик. Пишите @client12345", [], None),
    ("В личку не пишите @client12345", [], None),
    ("Написать", [{"label": "Написать", "url": "https://t.me/client12345"}], "client12345"),
    ("Форма отклика", [{"label": "Отклик", "url": "https://t.me/client12345?start=123"}], None),
    ("Отклик на work@example.org", [], None),
])
def test_outreach_only_uses_unambiguous_personal_contact(body, links, expected):
    channel = SimpleNamespace(username="job_feed")
    message = SimpleNamespace(text=body, metadata_json={"outbound_links": links})
    contact = contact_options(body, links, channel.username)
    assert outreach_username(message, channel, contact) == expected


@pytest.mark.asyncio
async def test_auto_application_reply_and_report(repo):
    channel = repo.candidate(100, "job_feed", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    now = datetime.now(timezone.utc)
    source = SimpleNamespace(outreach_recipient=AsyncMock(return_value=SimpleNamespace(id=555)),
                             send_outreach=AsyncMock(return_value=SimpleNamespace(id=80, date=now)))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))

    class ReplyAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "draft":
                return {"draft": "Здравствуйте! Сверстаю сайт для кафе. Пришлёте макет?",
                        "case_id": None, "draft_version": DRAFT_VERSION}
            return await super().ask(kind, text)

    service = ScoutService(repo, source, ReplyAI(), bot, 1)
    service.outreach.set_enabled(True)
    old = now - timedelta(hours=1)
    published = datetime.now(timezone.utc)
    body = "Нужен лендинг для кафе. Контакт для отклика: @client12345"
    for number, text, date in ((1, "Нужен сайт для врача. Контакт: @doctor1234", old),
                               (2, body, published), (3, body, published)):
        item = SimpleNamespace(id=number, raw_text=text, date=date, media=None, fwd_from=None)
        service._save_item(channel, item)
    await service.process_pending()
    await service.process_pending()
    source.outreach_recipient.assert_awaited_once_with("client12345")
    source.send_outreach.assert_awaited_once()
    sent = source.send_outreach.await_args.args[1]
    assert sent.startswith("Здравствуйте!")
    assert all(sent.count(case["url"]) == 1 for case in CASES.values())
    job = next(job for job in repo.jobs() if job.status == "applied")

    reply = SimpleNamespace(id=81, date=now + timedelta(seconds=1), raw_text="Вот макет", reply_to_msg_id=80)
    await service.outreach.on_reply(SimpleNamespace(is_private=True, sender_id=999, message=reply))
    assert repo.job(job.id).status == "applied"
    event = SimpleNamespace(is_private=True, sender_id=555, message=reply)
    await service.outreach.on_reply(event)
    await service.outreach.on_reply(event)
    assert repo.job(job.id).status == "replied"
    sent_notifications = [call.args[1] for call in bot.send_message.await_args_list if "Вот макет" in call.args[1]]
    assert len(sent_notifications) == 1
    assert "@client12345" in sent_notifications[0]
    report = service.outreach.report(now + timedelta(seconds=1))[0]
    assert "Отправлено 1 · ответов получено 1" in report
    assert "@client12345" in report and "https://t.me/job_feed/2" in report
    assert "@doctor1234" not in report


@pytest.mark.asyncio
async def test_generic_remote_job_is_not_sent(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class GenericSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text="Требуется чат-менеджер на удалённую работу, отвечать клиентам в Telegram",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, GenericSource(), FakeAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_not_awaited()
    assert repo.stats()["jobs"] == 0


@pytest.mark.asyncio
async def test_regular_it_work_does_not_require_vibe_coding(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class StrictAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": True,
                        "is_ongoing": True, "ai_coding_explicit": False, "reason": "найм"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, FakeSource(), StrictAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_awaited_once()
    assert repo.stats()["jobs"] == 1


@pytest.mark.asyncio
async def test_permanent_it_role_with_explicit_vibe_coding_is_sent(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class VibeSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                    yield SimpleNamespace(id=1, raw_text="Вакансия Python-разработчика в штат: писать ботов с помощью вайбкодинга и Codex, зарплата 120000 рублей. Пишите @client12345",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    class VibeAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": True,
                        "is_ongoing": True, "ai_coding_explicit": True, "reason": "разработка с Codex"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, VibeSource(), VibeAI(), bot, 1)
    repo.set_setting("employment_full_time_role", "true")
    await service.monitor()
    bot.send_message.assert_awaited_once()
    await service.monitor()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_time_is_disabled_by_default_even_without_ai_coding(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class OrdinaryRoleSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text="Вакансия Python-разработчика в штат: писать ботов для клиентов, зарплата 120000 рублей",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, OrdinaryRoleSource(), FakeAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_beginner_ai_full_time_role_has_its_own_toggle(repo, enabled):
    channel = repo.candidate(101, "ai_jobs", "AI Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class Source(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text=("Вакансия: начинающий AI-специалист в штат, "
                    "делать Telegram-ботов через Codex. Без опыта. Пишите @client12345"),
                    date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, Source(), FakeAI(), bot, 1)
    if not enabled:
        repo.set_setting("employment_beginner_ai_role", "false")
    await service.monitor()
    if enabled:
        bot.send_message.assert_awaited_once()
        assert "Вакансия" in bot.send_message.await_args.args[1]
        assert "Полная занятость" in bot.send_message.await_args.args[1]
    else:
        bot.send_message.assert_not_awaited()
    await service.monitor()
    assert bot.send_message.await_count == int(enabled)


@pytest.mark.asyncio
async def test_beginner_ai_vacancy_with_unconfirmed_application_form_is_labeled(repo):
    channel = repo.candidate(103, "ai_jobs_form", "AI Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class Source(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text=("Вакансия: начинающий AI-специалист, неполный день, "
                    "делать ботов через Codex. Отклик https://example.org/apply"),
                    date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, Source(), FakeAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_awaited_once()
    assert "Не подтверждено" in bot.send_message.await_args.args[1]
    assert "Форма отклика (условия не подтверждены)" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("announcement, should_send", [
    ("Подработка: поддерживать Python-бота по 3 часа в день, оплата ежемесячно", False),
    ("Подработка: поддерживать Python-бота по 3 часа в день с помощью Codex, оплата ежемесячно", True),
    ("Разовая подработка: написать Python-скрипт для выгрузки данных, оплата за результат", True),
])
async def test_part_time_and_one_off_without_ai_coding(repo, announcement, should_send):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class PartTimeSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text=announcement + ". Пишите заказчику @client12345",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    class PartTimeAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": True,
                        "is_ongoing": "Разовая" not in text,
                        "ai_coding_explicit": "Codex" in text, "reason": "IT-задача"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, PartTimeSource(), PartTimeAI(), bot, 1)
    await service.monitor()
    assert bot.send_message.await_count == int(should_send)


@pytest.mark.asyncio
async def test_explicit_vibe_coding_part_time_is_sent_when_ai_mislabels_it_and_phrase(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class VibeSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text="Подработка 3 часа в день: делайте задачи через вайб кодинг, оплата 50000 рублей в месяц. Пишите @client12345",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    class MislabelingAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": False, "codex_feasible": True,
                        "is_ongoing": True, "ai_coding_explicit": False, "reason": "оплачиваемая подработка"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, VibeSource(), MislabelingAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_not_awaited()
    assert repo.stats()["jobs"] == 0


@pytest.mark.asyncio
async def test_it_project_must_be_feasible_with_codex(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class HardProjectAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": False,
                        "is_ongoing": False, "ai_coding_explicit": False, "reason": "нужна команда"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, FakeSource(), HardProjectAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("announcement, reason", [
    ("Ищем Senior DevOps для настройки серверов и API. Пишите @client12345", "requires_verified_expertise"),
    ("Нужен Python-разработчик, работа только офис. Пишите @client12345", "office_required"),
])
async def test_high_risk_or_office_role_is_not_notified_even_with_permissive_ai(repo, announcement, reason):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class Source(FakeSource):
        async def messages(self, channel, min_id):
            yield SimpleNamespace(id=1, raw_text=announcement,
                                  date=datetime.now(timezone.utc), media=None, fwd_from=None)

    ai = SimpleNamespace(ask=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, Source(), ai, bot, 1)
    await service.monitor()
    assert repo.stats()["jobs"] == 0
    assert repo.rejection_counts() == [(reason, 1)]
    ai.ask.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_outage_keeps_cursor(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class BrokenSource(FakeSource):
        async def messages(self, channel, min_id):
            raise RuntimeError("Telegram недоступен")
            yield  # make async generator

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, BrokenSource(), FakeAI(), bot, 1)
    await service.monitor()
    assert repo.cursor(channel.id) == 0


@pytest.mark.asyncio
async def test_manual_add_survives_history_error(repo):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, FakeSource(), FakeAI(), bot, 1)
    channel = await service.add_channel("@scoutjobs")
    assert channel.status == "pending"
    assert repo.list_channels("approved") == []
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_manual_channel_can_be_reviewed_when_ai_finds_no_fit(repo):
    class CandidateSource(FakeSource):
        async def inspect(self, entity):
            return ["Вакансия администратора на постоянную работу с гибким графиком"], True, 0.0

    class SelectiveAI(FakeAI):
        async def ask(self, kind, text):
            return {"suitable": False, "reason": "постоянная работа"}

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, CandidateSource(), SelectiveAI(), bot, 1)
    channel = await service.add_channel("@scoutjobs")
    assert channel.status == "pending"
    candidate = repo.get_candidate(channel.id)
    assert candidate.usefulness == 10
    assert "0 из 1" in candidate.reason


@pytest.mark.asyncio
async def test_manual_kwork_channel_is_paused_until_enabled(repo):
    class KworkSource(FakeSource):
        async def resolve(self, username):
            return SimpleNamespace(id=100, username=username, title="Заказы Kwork", broadcast=True)

        async def inspect(self, entity):
            return ["Нужен сайт, отклик через Кворк"], True, 0.0

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, KworkSource(),
                           SimpleNamespace(ask=AsyncMock(return_value={"suitable": True})), bot, 1)
    channel = await service.add_channel("@scoutjobs")
    assert service.source_paused(channel)
    bot.send_message.assert_not_awaited()
    repo.set_setting("platform_sources_enabled", "true")
    assert not service.source_paused(channel)
    await service.notify_candidate(channel)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_suggests_recent_development_chats_across_queries(repo):
    class DiscoverySource(FakeSource):
        def __init__(self):
            self.searches = []

        async def post_search_quota(self, query):
            return SimpleNamespace(remains=10, query_is_free=False, wait_till=None)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            assert limit == 30
            self.searches.append(query)
            return [SimpleNamespace(id=i, username=f"jobs{i:05}", title=f"Chat {i}") for i in range(1, 5)]

        async def inspect(self, entity, max_age_days=30):
            assert max_age_days == 3
            examples = {
                1: "Нужен разработчик Python для интеграции API, оплата по договорённости. Пишите @client12345",
                2: "Нужен дизайнер для оформления логотипа, оплата 5000 рублей",
                3: "Требуется менеджер по продажам на удалёнку, оплата по договорённости",
                4: "Нужен разработчик бота на Python, оплата по договорённости",
            }
            return [examples[entity.id]], entity.id != 4, 0.0

    class DiscoveryAI(FakeAI):
        async def ask(self, kind, text):
            assert kind == "candidate"
            return {"suitable": True, "reason": "IT-заказ"}

    source = DiscoverySource()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, source, DiscoveryAI(), bot, 1)
    status, found, errors, retry = await service.discover()
    assert (status, found, errors) == ("done", 1, 0)
    assert retry is not None
    assert len(source.searches) == len({chat_search_term(phrase) for phrases in DISCOVERY_GROUPS.values() for phrase in phrases})
    assert [channel.username for channel in repo.list_channels("pending")] == ["jobs00001"]
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_checks_all_found_chats_without_model_call_limit(repo):
    class Source(FakeSource):
        async def post_search_quota(self, query):
            return SimpleNamespace(remains=10, query_is_free=False, wait_till=None)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            return [SimpleNamespace(id=i, username=f"jobs{i:05}", title=f"Chat {i}") for i in range(100, 103)]

        async def inspect(self, entity, max_age_days=30):
            return ["Нужен разработчик Python для интеграции API, оплата по договорённости. Пишите @client12345"], True, 0.0

    class AI:
        calls = 0

        async def ask(self, kind, text):
            self.calls += 1
            return {"suitable": False, "reason": "не подходит"}

    ai = AI()
    service = ScoutService(repo, Source(), ai, SimpleNamespace(send_message=AsyncMock()), 1)
    assert (await service.discover())[0] == "done"
    assert ai.calls == 3


@pytest.mark.asyncio
async def test_discovery_can_suggest_sources_with_unverified_contacts(repo):
    class DiscoverySource(FakeSource):
        async def post_search_quota(self, query):
            return SimpleNamespace(remains=0, query_is_free=True, wait_till=None)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            return [SimpleNamespace(id=i, username=f"jobs{i:05}", title=title)
                    for i, title in ((1, "Обычные заказы"), (2, "Заказы через Кворк"),
                                     (3, "Прямые заказы"), (4, "Разработка"))]

        async def inspect(self, entity, max_age_days=30):
            examples = {
                1: "Нужен разработчик сайта, бюджет 10000 рублей, подробности в канале",
                2: "Нужен разработчик сайта, отклик через Kwork.ru/task/123",
                3: "Нужен разработчик Python для API, пишите заказчику @client12345",
                4: "Нужен разработчик бота, заявку подать на кворке",
            }
            return [examples[entity.id]], True, 0.0

    ai = SimpleNamespace(ask=AsyncMock(return_value={"suitable": True}))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, DiscoverySource(), ai, bot, 1)
    assert (await service.discover())[:3] == ("done", 3, 0)
    candidates = repo.list_channels("pending")
    assert {channel.username for channel in candidates} == {"jobs00001", "jobs00003", "jobs00004"}
    assert repo.get_candidate(next(c.id for c in candidates if c.username == "jobs00003")).usefulness == 80
    digest = bot.send_message.await_args.args[1]
    assert "Прямые заказы" in digest
    assert "Обычные заказы" in digest


@pytest.mark.asyncio
async def test_monitor_does_not_notify_kwork_post(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class KworkSource(FakeSource):
        async def messages(self, channel, min_id):
            yield SimpleNamespace(id=1, raw_text="Нужен сайт для компании, отклик через Kwork.ru",
                                  date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock())
    ai = FakeAI()
    service = ScoutService(repo, KworkSource(), ai, bot, 1)
    await service.monitor()
    assert repo.stats()["jobs"] == 1
    assert repo.jobs()[0].contact_status == "none"
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_kwork_fast_monitor_sends_only_fresh_it_orders(repo):
    kwork = repo.candidate(100, "freelancers_kwork", "Kwork jobs", "", None, 70, "", [])
    other = repo.candidate(101, "other_jobs", "Other", "", None, 70, "", [])
    repo.transition_channel(kwork.id, "approved")
    repo.transition_channel(other.id, "approved")
    repo.set_setting("platform_sources_enabled", "true")
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    posts = [SimpleNamespace(id=1, raw_text="Нужен сайт для кафе, старый заказ Kwork",
                             date=old, media=None, fwd_from=None, entities=None,
                             buttons=[[SimpleNamespace(url="http://offersad.date/kw/?project=3273401", text="№ 3273401")]])]
    seen = []

    class Source:
        async def messages(self, channel, min_id):
            seen.append(channel.id)
            for post in posts:
                if post.id > min_id:
                    yield post

    kinds = []

    class AI(FakeAI):
        async def ask(self, kind, text):
            kinds.append(kind)
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, Source(), AI(), bot, 1)
    await service.monitor_kwork()
    assert seen == [kwork.id]
    assert kinds[:2] == ["kwork_prefilter", "deep"]
    bot.send_message.assert_not_awaited()

    posts.append(SimpleNamespace(id=2, raw_text="Нужен лендинг для кафе, новый заказ Kwork",
                                 date=datetime.now(timezone.utc), media=None, fwd_from=None, entities=None,
                                 buttons=[[SimpleNamespace(url="http://offersad.date/kw/?project=3273402", text="№ 3273402")]]))
    await service.monitor_kwork()
    bot.send_message.assert_awaited_once()
    assert repo.job_link(repo.jobs()[0].id) == "https://kwork.ru/projects/3273402/view"

    posts.append(SimpleNamespace(id=3, raw_text="Нужен бот для накрутки лайков и просмотров",
                                 date=datetime.now(timezone.utc), media=None, fwd_from=None, entities=None,
                                 buttons=[[SimpleNamespace(url="http://offersad.date/kw/?project=3273403", text="№ 3273403")]]))
    await service.monitor_kwork()
    assert repo.stats()["jobs"] == 2
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_today_sends_unconfirmed_job_once(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    post = SimpleNamespace(id=1, raw_text="Нужен сайт для компании, отклик через Kwork.ru",
                           date=datetime.now(timezone.utc), media=None, fwd_from=None, entities=None, buttons=None)

    class Source:
        async def messages_since(self, channel, since):
            yield post

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, Source(), FakeAI(), bot, 1)
    assert await service.backfill_today() == (1, 1, 1, 1, [])
    assert repo.jobs()[0].contact_status == "none"
    assert await service.backfill_today() == (1, 1, 0, 0, [])
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_platform_posts_and_sources_resume_from_menu_setting(repo):
    channel = repo.candidate(100, "scoutjobs", "Заказы Kwork", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class PlatformSource(FakeSource):
        calls = 0

        async def messages(self, channel, min_id):
            self.calls += 1
            yield SimpleNamespace(id=1, raw_text="Нужен сайт, отклик через FL.ru",
                                  date=datetime.now(timezone.utc), media=None, fwd_from=None)

    source = PlatformSource()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, source, FakeAI(), bot, 1)
    await service.monitor()
    assert source.calls == 0
    repo.set_setting("platform_sources_enabled", "true")
    await service.monitor()
    assert source.calls == 1
    assert repo.stats()["jobs"] == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_channel_sends_direct_contact_but_skips_fl(repo):
    channel = repo.candidate(100, "scoutjobs", "IT заказы", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class MixedSource(FakeSource):
        async def messages(self, channel, min_id):
            for number, text in enumerate((
                "Нужен Python бот, отклик через FL.ru",
                "Нужен Python бот для CRM, пишите заказчику @client12345",
            ), 1):
                yield SimpleNamespace(id=number, raw_text=text, date=datetime.now(timezone.utc),
                                      media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, MixedSource(), FakeAI(), bot, 1)
    await service.monitor()
    assert repo.stats()["jobs"] == 2
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_external_and_unknown_posts_are_assessed_but_not_notified(repo):
    channel = repo.candidate(100, "scoutjobs", "IT заказы", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class ExternalSource(FakeSource):
        async def messages(self, channel, min_id):
            for number, text in enumerate((
                "Нужен Python бот, отклик https://freelance.ru/task/view/123",
                "Нужен Python бот, подробности после перехода по ссылке",
            ), 1):
                yield SimpleNamespace(id=number, raw_text=text, date=datetime.now(timezone.utc),
                                      media=None, fwd_from=None)

    ai = FakeAI()
    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, ExternalSource(), ai, bot, 1)
    await service.monitor()
    assert repo.stats()["jobs"] == 2
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_waits_for_telegrams_quota_reset(repo):
    class DiscoverySource(FakeSource):
        def __init__(self):
            self.searches = 0

        async def post_search_quota(self, query):
            return SimpleNamespace(remains=1 if self.searches == 0 else 0,
                                   query_is_free=False, wait_till=int(datetime.now(timezone.utc).timestamp()) + 3600)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            self.searches += 1
            return []

    source = DiscoverySource()
    service = ScoutService(repo, source, FakeAI(), SimpleNamespace(), 1)
    status, found, errors, retry = await service.discover()
    assert (status, found, errors) == ("wait", 0, 0)
    assert retry > datetime.now(timezone.utc)
    assert source.searches == 1
    assert (await service.discover())[0] == "wait"
    assert source.searches == 1


@pytest.mark.asyncio
async def test_discovery_balances_tracks_across_quota_resets(repo):
    class Source(FakeSource):
        def __init__(self):
            self.attempts = 0
            self.searched = []

        async def post_search_quota(self, query):
            self.attempts += 1
            return SimpleNamespace(remains=int(self.attempts % 2 == 1), query_is_free=False,
                                   wait_till=int(datetime.now(timezone.utc).timestamp()) + 60)

        async def search_public_posts(self, query):
            self.searched.append(query)
            return []

        async def search_public_chats(self, query, limit=30):
            return []

    source = Source()
    service = ScoutService(repo, source, FakeAI(), SimpleNamespace(), 1)
    for _ in range(4):
        assert (await service.discover())[0] == "wait"
        repo.set_setting("discovery_retry_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    groups = {row.phrase: row.group for row in repo.search_queries()}
    assert [groups[query] == "vibe_work" for query in source.searched] == [False, True, False, True]


@pytest.mark.asyncio
async def test_discovery_obeys_flood_wait(repo):
    class FloodedSource(FakeSource):
        async def post_search_quota(self, query):
            raise FloodWaitError(None, capture=120)

    service = ScoutService(repo, FloodedSource(), FakeAI(), SimpleNamespace(), 1)
    status, found, errors, retry = await service.discover()
    assert (status, found, errors) == ("wait", 0, 0)
    assert retry >= datetime.now(timezone.utc) + timedelta(seconds=110)
    assert (await service.discover())[0] == "wait"


@pytest.mark.asyncio
async def test_discovery_stops_on_lost_telegram_connection(repo):
    class DisconnectedSource(FakeSource):
        def __init__(self):
            self.calls = 0

        async def post_search_quota(self, query):
            self.calls += 1
            raise ConnectionError("Cannot send requests while disconnected")

    source = DisconnectedSource()
    service = ScoutService(repo, source, FakeAI(), SimpleNamespace(), 1)
    assert (await service.discover())[:3] == ("error", 0, 1)
    assert source.calls == 1


@pytest.mark.asyncio
async def test_discovery_deduplicates_reposted_orders_across_chats(repo):
    order = "Нужен разработчик Python для интеграции API, оплата по договорённости. Пишите @client12345"

    class RepostSource(FakeSource):
        async def post_search_quota(self, query):
            return SimpleNamespace(remains=0, query_is_free=True, wait_till=None)

        async def search_public_posts(self, query):
            return [(SimpleNamespace(id=i, username=f"jobs{i:05}", title=f"Chat {i}"),
                     SimpleNamespace(raw_text=order, date=datetime.now(timezone.utc))) for i in (1, 2)]

        async def search_public_chats(self, query, limit=30):
            return []

        async def inspect(self, entity, max_age_days=30):
            return [order], True, 0.0

    ai = SimpleNamespace(ask=AsyncMock(return_value={"suitable": True}))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, RepostSource(), ai, bot, 1)
    await service.discover()
    assert len(repo.list_channels("pending")) == 1
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_sends_five_chat_digest_and_remaining_one(repo):
    class SixChatSource(FakeSource):
        def __init__(self):
            self.searches = 0

        async def post_search_quota(self, query):
            return SimpleNamespace(remains=1 if self.searches == 0 else 0, query_is_free=False,
                                   wait_till=int(datetime.now(timezone.utc).timestamp()) + 3600)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            self.searches += 1
            return [SimpleNamespace(id=i, username=f"jobs{i:05}", title=f"Chat {i}") for i in range(1, 7)]

        async def inspect(self, entity, max_age_days=30):
            orders = {
                1: "Нужен Python разработчик для API платежей, бюджет 15000 рублей",
                2: "Нужна верстка интернет магазина на React, оплата 25000 рублей",
                3: "Ищем исполнителя для разработки Telegram бота с оплатой через Юкассу",
                4: "Нужна автоматизация отчётов в CRM, бюджет обсуждается",
                5: "Требуется разработать мобильное приложение для доставки еды",
                6: "Заказ на парсинг каталога товаров, оплата по договорённости",
            }
            return [orders[entity.id] + ". Пишите заказчику @client12345"], True, 0.0

    ai = SimpleNamespace(ask=AsyncMock(return_value={"suitable": True}))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    service = ScoutService(repo, SixChatSource(), ai, bot, 1)
    assert (await service.discover())[:3] == ("wait", 6, 0)
    assert bot.send_message.await_count == 2
    first, second = [call.args[1] for call in bot.send_message.await_args_list]
    assert first.count("https://t.me/") == 5
    assert second.count("https://t.me/") == 1
    assert "Пример:" in first
    assert all(repo.get_candidate(channel.id).owner_notified for channel in repo.list_channels("pending"))
