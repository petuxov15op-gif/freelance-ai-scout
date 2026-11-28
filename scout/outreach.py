from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram.types import LinkPreviewOptions
from telethon.errors import PeerFloodError, RPCError

from .ai import draft_problem
from .core import synthetic_contact
from .models import utcnow
from .portfolio import CASES
from .telegram import USERNAME

log = logging.getLogger(__name__)
SETTINGS_KEY = "outreach_enabled_at"
LOCAL_ZONE = ZoneInfo("Asia/Yekaterinburg")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
INVITATION = re.compile(
    r"\b(?:пишите|напишите|написать|контакт\w*|связаться|отклик\w*|в\s+личк\w*|dm|apply|contact)\b"
    r"|(?:для\s+связи|связь\s*:|➡)", re.I
)
HANDLE = re.compile(r"(?<![\w/])(?:@|https://t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})\b(?![?#]|/[^\s.,)])", re.I)
NO_DMS = re.compile(r"\bне\s+(?:пишите|писать|откликайтесь|беспокойте|связывайтесь)\b", re.I)
UNAPPROVED_LINK = re.compile(r"(?:\w+://|\b(?:www|t)\.|\b[\w-]+\.(?:ru|com|org|net|app|io)\b)", re.I)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def outreach_username(message, channel, contact: dict) -> str | None:
    if contact["status"] == "paid" or synthetic_contact(message.text) or NO_DMS.search(message.text):
        return None
    metadata = message.metadata_json or {}
    names = set()
    for line in message.text.splitlines():
        if INVITATION.search(line):
            names.update(name.lower() for name in HANDLE.findall(line))
    for link in metadata.get("outbound_links", ()):
        if INVITATION.search(link.get("label", "")):
            name = USERNAME.fullmatch(link.get("url", ""))
            if name and link.get("url", "").startswith("https://t.me/"):
                names.add(name[1].lower())
    if re.search(r"\b(?:пишите|напишите|обращайтесь)\s+автору\b", message.text, re.I):
        author = metadata.get("author_username")
        if author:
            names.add(author.lower())
    names.discard((channel.username or "").lower())
    return next(iter(names)) if len(names) == 1 else None


class OutreachService:
    def __init__(self, repo, source, bot, owner_id: int, portfolio_urls: tuple[str, ...]):
        self.repo, self.source, self.bot, self.owner_id = repo, source, bot, owner_id
        self.portfolio_urls = portfolio_urls or tuple(case["url"] for case in CASES.values())
        self.lock = asyncio.Lock()
        self.notification_lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.repo.setting(SETTINGS_KEY))

    def set_enabled(self, enabled: bool) -> None:
        if self.enabled() != enabled:
            self.repo.set_setting(SETTINGS_KEY, utcnow().isoformat() if enabled else "")

    def target(self, channel, message, contact: dict) -> str | None:
        enabled_at = self.repo.setting(SETTINGS_KEY)
        if not enabled_at or channel.status != "approved":
            return None
        cutoff = max(datetime.fromisoformat(enabled_at), utcnow() - timedelta(minutes=30))
        if utc(message.published_at) < cutoff:
            return None
        return outreach_username(message, channel, contact)

    async def apply(self, job_id: int, channel, message, contact: dict, draft: dict | None) -> None:
        raw = draft.get("draft") if isinstance(draft, dict) else None
        if not isinstance(raw, str):
            return
        raw = raw.strip()
        for case in CASES.values():
            raw = raw.removesuffix("\n" + case["reply_line"] + " " + case["url"]).strip()
        if UNAPPROVED_LINK.search(raw) or draft_problem(raw, message.text):
            return
        text = raw + "\n\nПортфолио\n" + "\n".join(self.portfolio_urls)
        async with self.lock:
            username = self.target(channel, message, contact)
            if not username or not self.repo.start_outreach(job_id, message.id, username, text):
                return
            peer_id = None
            try:
                recipient = await asyncio.wait_for(self.source.outreach_recipient(username), timeout=15)
                if not self.enabled() or self.repo.setting("monitoring_paused") == "true":
                    self.repo.finish_outreach(job_id, None, problem="Отправка на паузе")
                    return
                peer_id = recipient.id
                sent = await asyncio.wait_for(self.source.send_outreach(recipient, text), timeout=20)
            except (RPCError, ValueError) as exc:
                problem = "Telegram отклонил отправку" if peer_id else "Контакт недоступен"
                self.repo.finish_outreach(job_id, peer_id, problem=problem)
                log.warning("Автоотклик по заказу %s отклонён: %s", job_id, type(exc).__name__)
                if isinstance(exc, PeerFloodError):
                    self.set_enabled(False)
                    await self.bot.send_message(self.owner_id, "Telegram ограничил отправку. Автоотклики на паузе.")
            except Exception as exc:
                problem = "Отправка не подтверждена" if peer_id else "Контакт временно недоступен"
                self.repo.finish_outreach(job_id, peer_id, problem=problem)
                log.warning("Автоотклик по заказу %s: %s", job_id, type(exc).__name__)
            else:
                self.repo.finish_outreach(job_id, peer_id, sent.id, sent.date)

    async def on_reply(self, event) -> None:
        if not event.is_private or event.sender_id is None:
            return
        message = event.message
        async with self.lock:
            recorded = self.repo.record_outreach_reply(
                event.sender_id, message.id, message.date,
                (message.raw_text or "Вложение или голосовое сообщение. Открой чат, чтобы посмотреть.")[:1500],
                message.reply_to_msg_id,
            )
        if recorded:
            await self.notify_replies()

    async def notify_replies(self) -> None:
        async with self.notification_lock:
            for reply, username, title in self.repo.pending_outreach_replies():
                await self.bot.send_message(
                    self.owner_id,
                    f"💬 Ответ на автоотклик от @{username}\n"
                    f"Заказ #{reply.job_id} · {title or 'Без названия'}\n"
                    f"https://t.me/{username}\n\n{reply.text}",
                    link_preview_options=NO_PREVIEW,
                )
                self.repo.finish_outreach_reply(reply.id)

    def report(self, now: datetime | None = None, include_empty: bool = True) -> list[str]:
        now = (now or utcnow()).astimezone(LOCAL_ZONE)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows, replies = self.repo.outreach_today(utc(start), utc(start + timedelta(days=1)))
        if not include_empty and not rows and not replies:
            return []
        sent = sum(row.sent_at is not None for row, _, _, _ in rows)
        status = "включены" if self.enabled() else "на паузе"
        heading = (f"✉️ Автоотклики за {start:%d.%m} (Екб)\n"
                   f"Отправлено {sent} · ответов получено {replies} · без подтверждения отправки {len(rows) - sent}")
        pages = [heading + f"\nАвтоотклики {status}."]
        for row, title, link, reply_count in rows:
            when = utc(row.sent_at or row.started_at).astimezone(LOCAL_ZONE).strftime("%H:%M")
            outcome = f"отправлено · ответов {reply_count}" if row.sent_at else row.problem or "Отправка не подтверждена"
            entry = f"\n\n{when} · @{row.username} · {outcome}\n#{row.job_id} {title or 'Без названия'}"
            if link:
                entry += "\n" + link
            if len(pages[-1]) + len(entry) > 1900:
                pages.append(heading)
            pages[-1] += entry
        return pages

    async def daily_report(self) -> None:
        for page in self.report(include_empty=self.enabled()):
            await self.bot.send_message(self.owner_id, page, link_preview_options=NO_PREVIEW)
