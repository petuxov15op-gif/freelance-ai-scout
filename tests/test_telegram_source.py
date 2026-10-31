from unittest.mock import AsyncMock

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
