from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from telethon.errors import FloodWaitError

from .core import (explicit_ai_coding, explicit_it_task, has_direct_contact, likely_job,
                   mentions_platform, ongoing_role, platform_only_post, potential_job, similar)
from .models import Channel
from .repository import Repository

log = logging.getLogger(__name__)

JOB_STATUS_LABELS = {
    "interesting": "⭐ Интересно",
    "irrelevant": "❌ Не подходит",
    "applied": "📨 Откликнулся",
    "won": "🏆 Получил заказ",
}

DISCOVERY_QUERIES = (
    "нужен разработчик", "нужно разработать", "ищу программиста", "заказ на разработку",
    "сделать сайт", "разработать сайт", "сделать бота", "разработать бота",
    "нужен python", "нужен frontend", "нужен backend", "интеграция api",
    "нужна автоматизация", "написать скрипт", "разработать приложение", "нужна верстка",
    "заказ на crm", "доработать сайт", "нужен программист", "заказ на бота",
    "заказы разработка", "заказы программистам", "фриланс разработка",
)
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
    def __init__(self, repo: Repository, source, ai, bot: Bot, owner_id: int,
                 discovery_ai_call_limit: int = 10):
        self.repo, self.source, self.ai, self.bot, self.owner_id = repo, source, ai, bot, owner_id
        self.discovery_ai_call_limit = discovery_ai_call_limit
        self.monitor_lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.last_monitor_started: datetime | None = None
        self.last_monitor_finished: datetime | None = None
        self.last_monitor_errors: list[str] = []

    def platforms_enabled(self) -> bool:
        return self.repo.setting("platform_sources_enabled", "false") == "true"

    def source_paused(self, channel: Channel | None) -> bool:
        return bool(channel) and not self.platforms_enabled() and mentions_platform(
            " ".join((channel.username or "", channel.title or "", channel.description or "")))

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
        if not candidate or candidate.owner_notified or self.source_paused(channel):
            return
        examples = "\n".join("• " + x[:180] for x in candidate.examples[:2]) or "Нет подходящих примеров"
        text = (f"Новый чат/канал: {channel.title}\n@{channel.username} · https://t.me/{channel.username}\n"
                f"Описание: {channel.description or 'не указано'}\n"
                f"Полезность: {candidate.usefulness}/100\n{candidate.reason}\nПримеры публикаций:\n{examples}")
        await self.bot.send_message(self.owner_id, text[:4000], reply_markup=candidate_keyboard(channel.id))
        self.repo.mark_candidate_notified(channel.id)

    async def notify_candidate_batch(self, channels: list[Channel]) -> None:
        from .bot import candidate_digest_keyboard
        entries = [(channel, self.repo.get_candidate(channel.id)) for channel in channels]
        entries = [(channel, candidate) for channel, candidate in entries
                   if candidate and not candidate.owner_notified
                   and not self.source_paused(channel)]
        if not entries:
            return
        entries.sort(key=lambda item: item[1].usefulness, reverse=True)
        lines = ["Новые чаты на рассмотрение:"]
        for number, (channel, candidate) in enumerate(entries, 1):
            example = " ".join(candidate.examples[0].split())[:220] if candidate.examples else "Пример не найден"
            lines.append(f"\n{number}. {channel.title[:90]} · {candidate.usefulness}/100\n"
                         f"https://t.me/{channel.username}\n"
                         f"Почему: {candidate.reason[:160]}\nПример: {example}")
        await self.bot.send_message(self.owner_id, "\n".join(lines)[:4000],
                                    reply_markup=candidate_digest_keyboard([channel.id for channel, _ in entries]))
        for channel, _ in entries:
            self.repo.mark_candidate_notified(channel.id)

    async def discover(self) -> tuple[str, int, int, datetime | None]:
        if self.discovery_lock.locked():
            return "running", 0, 0, None
        async with self.discovery_lock:
            now = datetime.now(timezone.utc)
            channels = [channel for status in ("approved", "pending", "rejected", "blocked")
                        for channel in self.repo.list_channels(status)]
            known = {channel.telegram_id for channel in channels}
            seen_examples: list[str] = []
            batch: list[Channel] = []
            for channel in channels:
                candidate = self.repo.get_candidate(channel.id)
                if (channel.status == "pending" and candidate and candidate.query and not candidate.owner_notified
                        and not self.source_paused(channel)):
                    batch.append(channel)
                if not candidate or not candidate.query or not candidate.examples:
                    continue
                discovered_at = candidate.discovered_at
                if discovered_at.tzinfo is None:
                    discovered_at = discovered_at.replace(tzinfo=timezone.utc)
                if discovered_at >= now - timedelta(days=3):
                    seen_examples.append(candidate.examples[0])
            suggested = 0
            errors = 0
            model_calls = 0

            async def flush_batch(all_items: bool = False) -> None:
                nonlocal errors
                while all_items and batch:
                    batch.sort(key=lambda channel: self.repo.get_candidate(channel.id).usefulness, reverse=True)
                    part = batch[:5]
                    try:
                        await self.notify_candidate_batch(part)
                    except Exception:
                        errors += 1
                        log.exception("Не удалось отправить подборку чатов")
                        break
                    del batch[:5]

            async def finish(status: str, retry_at: datetime | None):
                await flush_batch(all_items=True)
                return status, suggested, errors, retry_at

            retry = self.repo.setting("discovery_retry_at")
            if retry and datetime.fromisoformat(retry) > now:
                return await finish("wait", datetime.fromisoformat(retry))
            start = int(self.repo.setting("discovery_query_index", "0"))
            for offset in range(len(DISCOVERY_QUERIES)):
                index = (start + offset) % len(DISCOVERY_QUERIES)
                query = DISCOVERY_QUERIES[index]
                try:
                    quota = await self.source.post_search_quota(query)
                    if not quota.remains and not quota.query_is_free:
                        wait_until = (datetime.fromtimestamp(quota.wait_till, timezone.utc) if quota.wait_till
                                      else datetime.now(timezone.utc) + timedelta(hours=1))
                        self.repo.set_setting("discovery_retry_at", wait_until.isoformat())
                        log.info("Бесплатный поиск Telegram исчерпан; повтор после %s", wait_until.isoformat())
                        return await finish("wait", wait_until)
                    found = await self.source.search_public_posts(query)
                    try:
                        chats = await self.source.search_public_chats(query, limit=30)
                    except FloodWaitError:
                        raise
                    except Exception:
                        errors += 1
                        log.exception("Поиск публичных чатов по запросу %s не удался", query)
                        chats = []
                    self.repo.set_setting("discovery_query_index", str((index + 1) % len(DISCOVERY_QUERIES)))
                except FloodWaitError as exc:
                    wait_until = datetime.now(timezone.utc) + timedelta(seconds=max(exc.seconds, 60))
                    self.repo.set_setting("discovery_retry_at", wait_until.isoformat())
                    log.warning("Telegram ограничил поиск на %s секунд", exc.seconds)
                    return await finish("wait", wait_until)
                except Exception:
                    errors += 1
                    log.exception("Поиск публикаций по запросу %s не удался", query)
                    continue
                for entity, post in [*found, *((chat, None) for chat in chats)]:
                    content = (post.raw_text or "") if post else ""
                    if entity.id in known:
                        continue
                    if not self.platforms_enabled() and mentions_platform(entity.title or ""):
                        continue
                    if post and (post.date < datetime.now(timezone.utc) - timedelta(days=3)
                                 or not likely_job(content) or not DEVELOPMENT_TASK.search(content)):
                        continue
                    if post and any(similar(content, prior) for prior in seen_examples):
                        continue
                    known.add(entity.id)
                    try:
                        examples, active, ad_ratio = await self.source.inspect(entity, max_age_days=3)
                        if not active or ad_ratio > .5:
                            continue
                        description = await self.source.description(entity)
                        if not self.platforms_enabled() and mentions_platform(description or ""):
                            continue
                        examples = [text for text in dict.fromkeys([content, *examples])
                                    if likely_job(text) and DEVELOPMENT_TASK.search(text)
                                    and (self.platforms_enabled() or not platform_only_post(text))]
                        if examples and any(similar(examples[0], prior) for prior in seen_examples):
                            continue
                        verified = []
                        for example in examples[:3]:
                            if model_calls >= self.discovery_ai_call_limit:
                                log.info("Ручной поиск остановлен: достигнут лимит %s запросов к модели",
                                         self.discovery_ai_call_limit)
                                return await finish("limited", None)
                            model_calls += 1
                            if (await self.ai.ask("candidate", example))["suitable"]:
                                verified.append(example)
                                break
                        if not verified:
                            continue
                        direct = has_direct_contact(verified[0])
                        channel = self.repo.candidate(
                            entity.id, entity.username, entity.title, description, query,
                            80 if direct else 50,
                            (f"Недавние IT-заказы: {len(verified)}; чат активен за последние 3 дня; "
                             + ("есть прямой контакт заказчика" if direct else "прямой контакт не указан")), verified,
                        )
                        if channel:
                            batch.append(channel)
                            seen_examples.append(verified[0])
                            suggested += 1
                    except FloodWaitError as exc:
                        wait_until = datetime.now(timezone.utc) + timedelta(seconds=max(exc.seconds, 60))
                        self.repo.set_setting("discovery_retry_at", wait_until.isoformat())
                        log.warning("Telegram ограничил проверку чатов на %s секунд", exc.seconds)
                        return await finish("wait", wait_until)
                    except Exception:
                        errors += 1
                        log.exception("Проверка найденного чата @%s не удалась", entity.username)
            # If all distinct queries were free/cached, check again later for new posts.
            return await finish("done", datetime.now(timezone.utc) + timedelta(hours=1))

    async def monitor(self) -> None:
        if self.monitor_lock.locked():
            return
        async with self.monitor_lock:
            self.last_monitor_started = datetime.now(timezone.utc)
            self.last_monitor_errors = []
            try:
                channels = self.repo.list_channels("approved")
                channels = [channel for channel in channels if not self.source_paused(channel)]
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
                if ((not self.platforms_enabled() and platform_only_post(message.text))
                        or self.source_paused(self.repo.get_channel(message.channel_id))
                        or not potential_job(message.text)
                        or not (explicit_it_task(message.text) or (ongoing and ai_coding))
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
        if (not self.platforms_enabled() and platform_only_post(self.repo.job_original(job_id) or "")) or self.source_paused(self.repo.job_channel(job_id)):
            return
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
