import asyncio
from types import SimpleNamespace

import pytest

from scout.bot import OwnerMiddleware
from scout.telegram import parse_public_channel


@pytest.mark.asyncio
async def test_owner_only():
    middleware = OwnerMiddleware(123)
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    event = SimpleNamespace(from_user=SimpleNamespace(id=123))
    await middleware(handler, event, {})
    assert called
    called = False
    event.from_user.id = 999
    await middleware(handler, event, {})
    assert not called


def test_channel_input():
    assert parse_public_channel("https://t.me/publicjobs") == "publicjobs"
    with pytest.raises(ValueError):
        parse_public_channel("https://evil.example/publicjobs")
