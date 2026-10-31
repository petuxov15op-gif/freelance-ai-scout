from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from scout.telegram import TelegramSource


@pytest.mark.asyncio
async def test_authorized_session_does_not_require_phone(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    source.client = AsyncMock()
    source.client.is_user_authorized.return_value = True
    await source.start()
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
async def test_resolve_uses_cached_peer_instead_of_resolving_username_again(tmp_path):
    source = TelegramSource(12345, "fake-hash", str(tmp_path / "session"))
    source.client = AsyncMock()
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
        async def iter_messages(self, entity, limit):
            yield SimpleNamespace(date=now - timedelta(days=4), raw_text="Нужен сайт, оплата")

    source.client = Client()
    examples, active, ad_ratio = await source.inspect(object(), max_age_days=3)
    assert examples == []
    assert active is False
    assert ad_ratio == 0
