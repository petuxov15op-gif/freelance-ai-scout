from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, RPCError

from .core import likely_job, similar

log = logging.getLogger(__name__)
USERNAME = re.compile(r"^(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{4,31})/?$")


def parse_public_channel(value: str) -> str:
    match = USERNAME.fullmatch(value.strip())
    if not match:
        raise ValueError("Укажите публичный канал: @username или https://t.me/username")
    return match.group(1)


class TelegramSource:
    def __init__(self, api_id: int, api_hash: str, session_path: str, phone: str = ""):
        self.client = TelegramClient(session_path, api_id, api_hash)
        self.phone = phone

    async def start(self) -> None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return
        if not self.phone:
            raise RuntimeError("Telegram-сессия не создана. Выполните первый вход: python -m scout.auth")
        await self.client.start(phone=self.phone)

    async def close(self) -> None:
        await self.client.disconnect()

    async def resolve(self, username: str):
        # get_entity(username) always resolves the name again, even when the
        # session already knows the channel and Telegram has limited lookups.
        peer = await self.client.get_input_entity(username)
        entity = await self.client.get_entity(peer)
        if not isinstance(entity, types.Channel) or not entity.username:
            raise ValueError("Доступен только публичный Telegram-канал или чат")
        return entity

    async def search_public_chats(self, query: str, limit: int = 30):
        result = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        return [chat for chat in result.chats
                if isinstance(chat, types.Channel) and chat.username]

    async def post_search_quota(self, query: str):
        return await self.client(functions.channels.CheckSearchPostsFloodRequest(query=query))

    async def search_public_posts(self, query: str, limit: int = 100):
        result = await self.client(functions.channels.SearchPostsRequest(
            offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0,
            limit=limit, query=query,
        ))
        chats = {chat.id: chat for chat in result.chats
                 if isinstance(chat, types.Channel) and chat.username}
        return [(chats[message.peer_id.channel_id], message)
                for message in result.messages
                if isinstance(message.peer_id, types.PeerChannel)
                and message.peer_id.channel_id in chats]

    async def examples(self, entity, limit: int = 12) -> list[str]:
        examples, _, _ = await self.inspect(entity, limit)
        return examples

    async def inspect(self, entity, limit: int = 20, max_age_days: int = 30) -> tuple[list[str], bool, float]:
        result = []
        latest = None
        advertising = 0
        total = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        async for message in self.client.iter_messages(entity, limit=limit):
            if latest is None:
                latest = message.date
            if message.date < cutoff:
                break
            total += 1
            content = (message.raw_text or "").lower()
            if any(token in content for token in ("реклама", "рекламный пост", "подписывайтесь", "advertisement", "sponsored")):
                advertising += 1
            if message.raw_text and likely_job(message.raw_text) and not any(similar(message.raw_text, prior) for prior in result):
                result.append(message.raw_text[:800])
        active = bool(latest and latest >= cutoff)
        return result[:3], active, advertising / total if total else 0.0

    async def description(self, entity) -> str | None:
        try:
            full = await self.client(functions.channels.GetFullChannelRequest(entity))
            return getattr(full.full_chat, "about", None)
        except RPCError:
            return None

    async def latest_message_id(self, channel) -> int:
        async for message in self.client.iter_messages(channel.username, limit=1):
            return message.id
        return 0

    async def messages(self, channel, min_id: int):
        try:
            async for message in self.client.iter_messages(channel.username or channel.telegram_id, min_id=min_id, reverse=True):
                yield message
        except FloodWaitError as exc:
            log.warning("FloodWait %s: %s секунд", channel.title, exc.seconds)
            await asyncio.sleep(exc.seconds)
            raise
