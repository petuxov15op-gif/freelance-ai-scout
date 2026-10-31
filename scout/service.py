from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from .core import explicit_ai_coding, explicit_it_task, likely_job, ongoing_role, potential_job
from .models import Channel
from .repository import Repository

log = logging.getLogger(__name__)

JOB_STATUS_LABELS = {
    "interesting": "⭐ Интересно",
    "irrelevant": "❌ Не подходит",
    "applied": "📨 Откликнулся",
    "won": "🏆 Получил заказ",
}

DISCOVERY_QUERIES = ("заказы разработка", "заказы программистам", "фриланс разработка")
DEVELOPMENT_TASK = re.compile(
    r"\b(?:разработ|программ|сайт|лендинг|верстк|бот\b|автоматизац|интеграц|парсинг|"
    r"скрипт|приложен|crm\b|api\b|python\b|frontend\b|backend\b|wordpress\b|tilda\b)",
    re.IGNORECASE,
)


def job_text(job, analysis, link: str | None) -> str:
    result = analysis.result
    requirements = ", ".join(result["requirements"][:4]) or "не указаны"
    status = JOB_STATUS_LABELS.get(job.status)
    return (f"{status + ' · ' if status else ''}Заказ #{job.id}: {result['title']}\n"
            f"Рейтинг: {job.score}/100 · {result['category']}\n"
            f"Бюджет: {result['budget'] or 'не указан'} {result['currency'] or ''}\n"
            f"Требования: {requirements}\n"
            f"Почему: {result['score_reason'][:500]}\n"
            f"Источник: {link or 'ссылка недоступна'}")


class ScoutService:
    def __init__(self, repo: Repository, source, ai, bot: Bot, owner_id: int):
        self.repo, self.source, self.ai, self.bot, self.owner_id = repo, source, ai, bot, owner_id
        self.monitor_lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.last_monitor_started: datetime | None = None
        self.last_monitor_finished: datetime | None = None
        self.last_monitor_errors: list[str] = []

    async def add_channel(self, raw: str) -> Channel | None:
        from .telegram import parse_public_channel
        username = parse_public_channel(raw)
        entity = await self.source.resolve(username)
        try:
            examples, active, ad_ratio = await self.source.inspect(entity)
        except Exception:
            log.exception("История канала %s недоступна; ручное добавление продолжено", username)
            examples, active, ad_ratio = [], False, 0.0
        verified = []
        for example in examples:
            try:
                assessment = await self.ai.ask("candidate", example)
            except Exception:
                log.exception("Оценка ручного источника %s не удалась", username)
                break
            if assessment["suitable"]:
                verified.append(example)
        usefulness = max(0, min(100, len(verified) * 30 + (10 if active else 0) - (30 if ad_ratio > .5 else 0)))
        reason = f"OpenRouter подтвердил {len(verified)} из {len(examples)} проверенных примеров; решение за вами"
        if not active:
            reason += "; канал неактивен более 30 дней"
        if ad_ratio > .5:
            reason += "; много рекламы"
        if verified:
            examples = verified
        description = await self.source.description(entity)
        channel = self.repo.candidate(entity.id, entity.username, entity.title, description, None, usefulness, reason, examples)
        if channel:
            await self.notify_candidate(channel)
        return channel

    async def notify_candidate(self, channel: Channel) -> None:
        from .bot import candidate_keyboard
        candidate = self.repo.get_candidate(channel.id)
        if not candidate or candidate.owner_notified:
            return
        examples = "\n".join("• " + x[:180] for x in candidate.examples[:2]) or "Нет подходящих примеров"
        text = (f"Новый чат/канал: {channel.title}\n@{channel.username} · https://t.me/{channel.username}\n"
                f"Описание: {channel.description or 'не указано'}\n"
                f"Полезность: {candidate.usefulness}/100\n{candidate.reason}\nПримеры публикаций:\n{examples}")
        await self.bot.send_message(self.owner_id, text[:4000], reply_markup=candidate_keyboard(channel.id))
        self.repo.mark_candidate_notified(channel.id)

    async def discover(self) -> None:
        if self.discovery_lock.locked():
            return
        async with self.discovery_lock:
            today = datetime.now(ZoneInfo("Asia/Yekaterinburg")).date().isoformat()
            if self.repo.setting("last_discovery_date") == today:
                return
            # Reserve this day's run before Telegram requests, including across restarts.
            self.repo.set_setting("last_discovery_date", today)
            known = {channel.telegram_id for status in ("approved", "pending", "rejected", "blocked")
                     for channel in self.repo.list_channels(status)}
            for query in DISCOVERY_QUERIES:
                reviewed_in_query = 0
                try:
                    found = await self.source.search_public_chats(query, limit=30)
                except Exception:
                    log.exception("Поиск чатов по запросу %s не удался", query)
                    continue
                for entity in found:
                    if entity.id in known or reviewed_in_query >= 4:
                        continue
                    known.add(entity.id)
                    reviewed_in_query += 1
                    try:
                        examples, active, ad_ratio = await self.source.inspect(entity, max_age_days=3)
                        if not active or ad_ratio > .5:
                            continue
                        examples = [text for text in examples if likely_job(text) and DEVELOPMENT_TASK.search(text)]
                        verified = []
                        for example in examples[:3]:
                            if (await self.ai.ask("candidate", example))["suitable"]:
                                verified.append(example)
                        if not verified:
                            continue
                        description = await self.source.description(entity)
                        channel = self.repo.candidate(
                            entity.id, entity.username, entity.title, description, query,
                            min(100, len(verified) * 30 + 20),
                            f"Недавние IT-заказы: {len(verified)}; чат активен за последние 3 дня", verified,
                        )
                        if channel:
                            await self.notify_candidate(channel)
                    except Exception:
                        log.exception("Проверка найденного чата @%s не удалась", entity.username)

    async def monitor(self) -> None:
        if self.monitor_lock.locked():
            return
        async with self.monitor_lock:
            self.last_monitor_started = datetime.now(timezone.utc)
            self.last_monitor_errors = []
            try:
                channels = self.repo.list_channels("approved")
                log.info("Мониторинг начат: одобренных каналов %s", len(channels))
                for channel in channels:
                    try:
                        async for item in self.source.messages(channel, self.repo.cursor(channel.id)):
                            link = f"https://t.me/{channel.username}/{item.id}" if channel.username else None
                            self.repo.save_message(channel.id, item.id, item.raw_text or "", link, item.date,
                                                   {"has_media": bool(item.media), "forwarded": bool(item.fwd_from)})
                            await asyncio.sleep(.3)
                    except Exception as exc:
                        self.last_monitor_errors.append(f"@{channel.username or channel.id}: {type(exc).__name__}")
                        log.exception("Мониторинг канала %s прерван, курсор сохранён", channel.title)
                await self.process_pending()
                log.info("Мониторинг завершён: одобренных каналов %s", len(channels))
            except Exception as exc:
                self.last_monitor_errors.append(f"Общий сбой: {type(exc).__name__}")
                raise
            finally:
                self.last_monitor_finished = datetime.now(timezone.utc)

    async def process_pending(self) -> None:
        self.repo.recover_stale_notifications()
        for message in self.repo.pending_messages():
            try:
                ongoing = ongoing_role(message.text)
                ai_coding = explicit_ai_coding(message.text)
                if (not potential_job(message.text) or not (explicit_it_task(message.text) or (ongoing and ai_coding))
                        or (ongoing and not ai_coding)
                        or self.repo.previously_irrelevant(message)):
                    self.repo.mark_message(message.id, "irrelevant")
                    continue
                existing = self.repo.matching_job(message)
                if existing and self.repo.analysis(existing.id):
                    self.repo.attach_job(message)
                    self.repo.mark_message(message.id, "linked")
                    continue
                pre = await self.ai.ask("prefilter", message.text)
                if (not (pre["is_job"] and pre["codex_feasible"] and (pre["is_it"] or (ongoing and ai_coding)))
                        or ((ongoing or pre["is_ongoing"]) and not ai_coding)):
                    self.repo.mark_message(message.id, "irrelevant")
                    continue
                job, created = self.repo.attach_job(message)
                if not created and self.repo.analysis(job.id):
                    self.repo.mark_message(message.id, "linked")
                    continue
                deep = await self.ai.ask("deep", message.text)
                self.repo.save_analysis(job.id, "deep", self.ai.model, deep)
                self.repo.mark_message(message.id, "linked")
                await self.notify_job(job.id, "instant")
            except Exception as exc:
                self.last_monitor_errors.append(f"Обработка объявления: {type(exc).__name__}")
                log.exception("Обработка объявления %s прервана; повторим позже", message.id)
        for job in self.repo.pending_instant_jobs(0):
            if self.repo.analysis(job.id):
                try:
                    await self.notify_job(job.id, "instant")
                except Exception as exc:
                    self.last_monitor_errors.append(f"Уведомление: {type(exc).__name__}")
                    log.exception("Повторное уведомление заказа %s не удалось", job.id)

    async def notify_job(self, job_id: int, kind: str) -> None:
        from .bot import job_keyboard
        if not self.repo.claim_notification(job_id, kind):
            return
        try:
            job = self.repo.job(job_id)
            analysis = self.repo.analysis(job_id)
            sent = await self.bot.send_message(self.owner_id, job_text(job, analysis, self.repo.job_link(job_id))[:4000],
                                               reply_markup=job_keyboard(job_id, self.repo.job_link(job_id)))
            self.repo.finish_notification(job_id, kind, sent.message_id)
        except Exception:
            self.repo.release_notification(job_id, kind)
            raise
