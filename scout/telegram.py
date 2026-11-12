from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, RPCError

from .core import contact_options, likely_job, similar

log = logging.getLogger(__name__)
USERNAME = re.compile(r"^(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{4,31})/?$")


@dataclass
class Inspection:
    examples: list[str]
    active: bool
    ad_ratio: float
    repeat_ratio: float
    contact_ratio: float

    def __iter__(self):
        # Preserve the existing three-value caller contract.
        yield self.examples
        yield self.active
        yield self.ad_ratio


def parse_public_channel(value: str) -> str:
    match = USERNAME.fullmatch(value.strip())
    if not match:
        raise ValueError("Укажите публичный канал: @username или https://t.me/username")
    return match.group(1)


def chat_search_term(query: str) -> str:
    """contacts.search matches substrings, not full-text job descriptions."""
    lower = query.lower()
    for term in ("python", "разработ", "программ", "автоматизац", "интеграц", "telegram",
                 "developer", "freelance", "frontend", "backend", "приложен", "сайт", "скрипт",
                 "бот", "crm", "api", "ваканс", "подрядчик"):
        if term in lower:
            return term
    return query


class TelegramSource:
    def __init__(self, api_id: int, api_hash: str, session_path: str, phone: str = ""):
        self.client = TelegramClient(session_path, api_id, api_hash)
        self.phone = phone
        self._connect_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            return
        # A background reader must never trigger interactive login or SMS retries.
        raise RuntimeError("Telegram-сессия не подтверждена. Проверьте подключение или выполните вход: python -m scout.auth")

    async def close(self) -> None:
        await self.client.disconnect()

    async def ensure_connected(self) -> None:
        if self.client.is_connected():
            return
        async with self._connect_lock:
            if not self.client.is_connected():
                log.warning("Соединение с Telegram потеряно; подключаюсь заново")
                await self.start()

    async def resolve(self, username: str):
        await self.ensure_connected()
        # get_entity(username) always resolves the name again, even when the
        # session already knows the channel and Telegram has limited lookups.
        peer = await self.client.get_input_entity(username)
        entity = await self.client.get_entity(peer)
        if not isinstance(entity, types.Channel) or not entity.username:
            raise ValueError("Доступен только публичный Telegram-канал или чат")
        return entity

    async def search_public_chats(self, query: str, limit: int = 30):
        await self.ensure_connected()
        result = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        return [chat for chat in result.chats
                if isinstance(chat, types.Channel) and chat.username]

    async def post_search_quota(self, query: str):
        await self.ensure_connected()
        return await self.client(functions.channels.CheckSearchPostsFloodRequest(query=query))

    async def search_public_posts(self, query: str, limit: int = 100):
        await self.ensure_connected()
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

    async def inspect(self, entity, limit: int = 20, max_age_days: int = 30) -> Inspection:
        await self.ensure_connected()
        result = []
        latest = None
        advertising = 0
        total = 0
        repeats = 0
        jobs = 0
        reachable = 0
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
            if message.raw_text and likely_job(message.raw_text):
                jobs += 1
                if any(similar(message.raw_text, prior) for prior in result):
                    repeats += 1
                else:
                    result.append(message.raw_text[:800])
                if contact_options(message.raw_text, outbound_links(message), getattr(entity, "username", None))["status"] in {"direct_free", "platform_free"}:
                    reachable += 1
        active = bool(latest and latest >= cutoff)
        return Inspection(result[:3], active, advertising / total if total else 0.0,
                          repeats / jobs if jobs else 0.0, reachable / jobs if jobs else 0.0)

    async def description(self, entity) -> str | None:
        await self.ensure_connected()
        try:
            full = await self.client(functions.channels.GetFullChannelRequest(entity))
            return getattr(full.full_chat, "about", None)
        except RPCError:
            return None

    async def latest_message_id(self, channel) -> int:
        await self.ensure_connected()
        async for message in self.client.iter_messages(channel.username or channel.telegram_id, limit=1):
            return message.id
        return 0

    async def messages(self, channel, min_id: int):
        await self.ensure_connected()
        try:
            async for message in self.client.iter_messages(channel.username or channel.telegram_id, min_id=min_id, reverse=True):
                yield message
        except FloodWaitError as exc:
            log.warning("FloodWait %s: %s секунд", channel.title, exc.seconds)
            await asyncio.sleep(exc.seconds)
            raise

    async def messages_since(self, channel, since: datetime):
        await self.ensure_connected()
        recent = []
        async for message in self.client.iter_messages(channel.username or channel.telegram_id):
            if message.date < since:
                break
            recent.append(message)
        for message in reversed(recent):
            yield message


def outbound_links(message) -> list[dict[str, str]]:
    result = []
    text = message.raw_text or ""
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            encoded = text.encode("utf-16-le")
            label = encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")
            result.append({"label": label, "url": url})
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if button.url:
                result.append({"label": button.text or "", "url": button.url})
    return result
