from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError

from scout.bot import create_router, job_keyboard
from scout.service import DISCOVERY_QUERIES, ScoutService


class FakeSource:
    async def messages(self, channel, min_id):
        if min_id < 1:
            yield SimpleNamespace(id=1, raw_text="Нужен сайт для небольшой компании, оплата и детали по договоренности",
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
        if kind == "prefilter":
            return {"is_job": True, "is_it": True, "codex_feasible": True,
                    "is_ongoing": False, "ai_coding_explicit": False, "reason": "заказ"}
        return {"title": "Сайт", "category": "сайт", "requirements": ["страница"],
                "budget": None, "currency": None, "deadline": None,
                "score_reason": "выполнимо", "score": 85}


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
    callback = SimpleNamespace(message=message, answer=AsyncMock())

    assert "Не подходит" in str(job_keyboard(job.id, None))
    callback.data = f"feedback:irrelevant:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "irrelevant"
    assert message.edit_text.await_args.args[0].startswith(f"❌ Не подходит · Заказ #{job.id}")
    assert "Изменить решение" in str(message.edit_text.await_args.kwargs["reply_markup"])
    assert "feedback:irrelevant" not in str(message.edit_text.await_args.kwargs["reply_markup"])

    callback.data = f"feedback:change:{job.id}"
    await callback_handler(callback)
    assert "Снять отметку" in str(message.edit_reply_markup.await_args.kwargs["reply_markup"])

    callback.data = f"feedback:interesting:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "interesting"
    assert message.edit_text.await_args.args[0].startswith(f"⭐ Интересно · Заказ #{job.id}")

    callback.data = f"feedback:new:{job.id}"
    await callback_handler(callback)
    assert repo.job(job.id).status == "new"
    assert message.edit_text.await_args.args[0].startswith(f"Заказ #{job.id}")


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
async def test_ai_rejects_permanent_it_role_without_vibe_coding(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class StrictAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": True,
                        "is_ongoing": True, "ai_coding_explicit": False, "reason": "найм"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, FakeSource(), StrictAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_not_awaited()
    assert repo.stats()["jobs"] == 0


@pytest.mark.asyncio
async def test_permanent_it_role_with_explicit_vibe_coding_is_sent(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class VibeSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text="Вакансия Python-разработчика в штат: писать ботов с помощью вайбкодинга и Codex, зарплата 120000 рублей",
                                      date=datetime.now(timezone.utc), media=None, fwd_from=None)

    class VibeAI(FakeAI):
        async def ask(self, kind, text):
            if kind == "prefilter":
                return {"is_job": True, "is_it": True, "codex_feasible": True,
                        "is_ongoing": True, "ai_coding_explicit": True, "reason": "разработка с Codex"}
            return await super().ask(kind, text)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    service = ScoutService(repo, VibeSource(), VibeAI(), bot, 1)
    await service.monitor()
    bot.send_message.assert_awaited_once()
    await service.monitor()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_it_role_without_explicit_ai_coding_is_not_sent(repo):
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
@pytest.mark.parametrize("announcement, should_send", [
    ("Подработка: поддерживать Python-бота по 3 часа в день, оплата ежемесячно", False),
    ("Подработка: поддерживать Python-бота по 3 часа в день с помощью Codex, оплата ежемесячно", True),
    ("Разовая подработка: написать Python-скрипт для выгрузки данных, оплата за результат", True),
])
async def test_part_time_requires_explicit_ai_coding_except_one_off(repo, announcement, should_send):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class PartTimeSource(FakeSource):
        async def messages(self, channel, min_id):
            if min_id < 1:
                yield SimpleNamespace(id=1, raw_text=announcement,
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
                yield SimpleNamespace(id=1, raw_text="Подработка 3 часа в день: делайте задачи через вайб кодинг, оплата 50000 рублей в месяц",
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
    bot.send_message.assert_awaited_once()


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
async def test_manual_kwork_channel_is_not_offered(repo):
    class KworkSource(FakeSource):
        async def inspect(self, entity):
            return ["Нужен сайт, отклик через Кворк"], True, 0.0

    bot = SimpleNamespace(send_message=AsyncMock())
    service = ScoutService(repo, KworkSource(), FakeAI(), bot, 1)
    with pytest.raises(ValueError, match="Kwork"):
        await service.add_channel("@scoutjobs")
    assert repo.list_channels("pending") == []
    bot.send_message.assert_not_awaited()


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
                1: "Нужен разработчик Python для интеграции API, оплата по договорённости",
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
    assert len(source.searches) == len(DISCOVERY_QUERIES)
    assert [channel.username for channel in repo.list_channels("pending")] == ["jobs00001"]
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_discovery_stops_at_model_call_limit(repo):
    class Source(FakeSource):
        async def post_search_quota(self, query):
            return SimpleNamespace(remains=10, query_is_free=False, wait_till=None)

        async def search_public_posts(self, query):
            return []

        async def search_public_chats(self, query, limit=30):
            return [SimpleNamespace(id=i, username=f"jobs{i:05}", title=f"Chat {i}") for i in range(100, 103)]

        async def inspect(self, entity, max_age_days=30):
            return ["Нужен разработчик Python для интеграции API, оплата по договорённости"], True, 0.0

    class AI:
        calls = 0

        async def ask(self, kind, text):
            self.calls += 1
            return {"suitable": False, "reason": "не подходит"}

    ai = AI()
    service = ScoutService(repo, Source(), ai, SimpleNamespace(send_message=AsyncMock()), 1, 2)
    assert (await service.discover())[0] == "limited"
    assert ai.calls == 2


@pytest.mark.asyncio
async def test_discovery_excludes_kwork_and_prioritizes_direct_contact(repo):
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
    assert (await service.discover())[:3] == ("done", 2, 0)
    candidates = repo.list_channels("pending")
    assert {channel.username for channel in candidates} == {"jobs00001", "jobs00003"}
    assert repo.get_candidate(next(c.id for c in candidates if c.username == "jobs00003")).usefulness == 80
    digest = bot.send_message.await_args.args[1]
    assert digest.index("Прямые заказы") < digest.index("Обычные заказы")


@pytest.mark.asyncio
async def test_monitor_does_not_notify_kwork_post(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")

    class KworkSource(FakeSource):
        async def messages(self, channel, min_id):
            yield SimpleNamespace(id=1, raw_text="Нужен сайт для компании, отклик через Kwork.ru",
                                  date=datetime.now(timezone.utc), media=None, fwd_from=None)

    bot = SimpleNamespace(send_message=AsyncMock())
    ai = SimpleNamespace(ask=AsyncMock())
    service = ScoutService(repo, KworkSource(), ai, bot, 1)
    await service.monitor()
    assert repo.stats()["jobs"] == 0
    ai.ask.assert_not_awaited()
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
async def test_discovery_deduplicates_reposted_orders_across_chats(repo):
    order = "Нужен разработчик Python для интеграции API, оплата по договорённости"

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
            return [orders[entity.id]], True, 0.0

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
