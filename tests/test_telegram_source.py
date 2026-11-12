import asyncio
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from scout.telegram import TelegramSource, chat_search_term, outbound_links


def test_chat_search_uses_substrings_suitable_for_contact_search():
    assert chat_search_term("ищем разработчика на проект") == "разработ"
    assert chat_search_term("looking for developer") == "developer"
    assert chat_search_term("интеграция с CRM") == "интеграц"


def test_outbound_links_reads_hidden_text_and_buttons():
    text = "🔥 Заявка"
    message = SimpleNamespace(raw_text=text,
                              entities=[types.MessageEntityTextUrl(offset=3, length=6, url="https://fl.ru/projects/1")],
                              buttons=[[SimpleNamespace(text="Написать", url="https://t.me/client12345")]])
    assert outbound_links(message) == [
        {"label": "Заявка", "url": "https://fl.ru/projects/1"},
        {"label": "Написать", "url": "https://t.me/client12345"},
    ]


@pytest.mark.asyncio
async def test_authorized_session_does_not_require_phone(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    source.client = AsyncMock()
    source.client.is_user_authorized.return_value = True
    await source.start()
    source.client.connect.assert_awaited_once()
    source.client.start.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_session_reconnects_once_for_concurrent_requests(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    connected = False

    async def connect():
        nonlocal connected
        connected = True

    source.client = AsyncMock()
    source.client.is_connected = lambda: connected
    source.client.connect.side_effect = connect
    source.client.is_user_authorized.return_value = True
    source.client.return_value = SimpleNamespace(remains=1)

    await asyncio.gather(source.post_search_quota("python"), source.post_search_quota("bot"))

    source.client.connect.assert_awaited_once()
    source.client.start.assert_not_called()


@pytest.mark.asyncio
async def test_missing_session_gives_setup_instruction(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    source.client = AsyncMock()
    source.client.is_user_authorized.return_value = False
    with pytest.raises(RuntimeError, match="python -m scout.auth"):
        await source.start()


@pytest.mark.asyncio
async def test_background_reader_never_starts_interactive_login(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"), phone="test-phone")
    source.client = AsyncMock()
    source.client.is_user_authorized.return_value = False
    with pytest.raises(RuntimeError, match="python -m scout.auth"):
        await source.start()
    source.client.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_uses_cached_peer_instead_of_resolving_username_again(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    source.client = AsyncMock()
    source.client.is_connected = lambda: True
    peer = types.InputPeerChannel(channel_id=123, access_hash=456)
    channel = types.Channel(id=123, title="Jobs", username="job_for_bots", access_hash=456, photo=None, date=None)
    source.client.get_input_entity.return_value = peer
    source.client.get_entity.return_value = channel

    assert await source.resolve("job_for_bots") is channel
    source.client.get_input_entity.assert_awaited_once_with("job_for_bots")
    source.client.get_entity.assert_awaited_once_with(peer)


@pytest.mark.asyncio
async def test_inspect_rejects_stale_chat_and_old_examples(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    now = datetime.now(timezone.utc)

    class Client:
        def is_connected(self):
            return True

        async def iter_messages(self, entity, limit):
            yield SimpleNamespace(date=now - timedelta(days=4), raw_text="Нужен сайт, оплата")

    source.client = Client()
    examples, active, ad_ratio = await source.inspect(object(), max_age_days=3)
    assert examples == []
    assert active is False
    assert ad_ratio == 0


@pytest.mark.asyncio
async def test_inspect_reports_repeats_and_reachable_posts(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    now = datetime.now(timezone.utc)
    posts = [
        "Нужен Python-бот для заявок, бюджет 20000. Пишите @client12345",
        "Нужен Python-бот для заявок, бюджет 20000. Пишите @client12345",
        "Нужен сайт для кафе, бюджет 30000. Подробнее в канале",
    ]

    class Client:
        def is_connected(self):
            return True

        async def iter_messages(self, entity, limit):
            for body in posts:
                yield SimpleNamespace(date=now, raw_text=body, entities=None, buttons=None)

    source.client = Client()
    inspected = await source.inspect(SimpleNamespace(username="jobs00001"))
    examples, active, ad_ratio = inspected
    assert len(examples) == 2 and active and ad_ratio == 0
    assert inspected.repeat_ratio == pytest.approx(1 / 3)
    assert inspected.contact_ratio == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_messages_since_reads_every_post_today_oldest_first(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    start = datetime(2026, 11, 6, tzinfo=timezone.utc)

    class Client:
        def is_connected(self):
            return True

        async def iter_messages(self, entity):
            for number, date in [(4, start + timedelta(hours=2)),
                                 (3, start + timedelta(hours=1)),
                                 (2, start), (1, start - timedelta(seconds=1))]:
                yield SimpleNamespace(id=number, date=date)

    source.client = Client()
    channel = SimpleNamespace(username="scoutjobs", telegram_id=100)
    assert [message.id async for message in source.messages_since(channel, start)] == [2, 3, 4]


@pytest.mark.asyncio
async def test_global_post_search_never_authorizes_stars(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    channel = types.Channel(id=123, title="Jobs", username="job_for_bots", access_hash=456, photo=None, date=None)
    post = SimpleNamespace(peer_id=types.PeerChannel(123), raw_text="Нужен разработчик", date=datetime.now(timezone.utc))
    source.client = AsyncMock(return_value=SimpleNamespace(chats=[channel], messages=[post]))
    source.client.is_connected = lambda: True

    assert await source.search_public_posts("нужен разработчик") == [(channel, post)]
    request = source.client.await_args.args[0]
    assert request.query == "нужен разработчик"
    assert request.allow_paid_stars is None


@pytest.mark.asyncio
async def test_post_search_quota_uses_telegram_counter(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    quota = SimpleNamespace(total_daily=10, remains=4, wait_till=None, query_is_free=False)
    source.client = AsyncMock(return_value=quota)
    source.client.is_connected = lambda: True
    assert await source.post_search_quota("нужен разработчик") is quota
    assert source.client.await_args.args[0].query == "нужен разработчик"
