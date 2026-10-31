from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot

from .core import explicit_ai_coding, explicit_it_task, ongoing_role, potential_job
from .models import Channel
from .repository import Repository

log = logging.getLogger(__name__)


def job_text(job, analysis, link: str | None) -> str:
    result = analysis.result
    requirements = ", ".join(result["requirements"][:4]) or "не указаны"
    return (f"Заказ #{job.id}: {result['title']}\n"
            f"Рейтинг: {job.score}/100 · {result['category']}\n"
            f"Бюджет: {result['budget'] or 'не указан'} {result['currency'] or ''}\n"
            f"Требования: {requirements}\n"
            f"Почему: {result['score_reason'][:500]}\n"
            f"Источник: {link or 'ссылка недоступна'}")


class ScoutService:
    def __init__(self, repo: Repository, source, ai, bot: Bot, owner_id: int):
        self.repo, self.source, self.ai, self.bot, self.owner_id = repo, source, ai, bot, owner_id
        self.monitor_lock = asyncio.Lock()
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
        text = (f"Новый канал: {channel.title}\n@{channel.username} · https://t.me/{channel.username}\n"
                f"Описание: {channel.description or 'не указано'}\n"
                f"Полезность: {candidate.usefulness}/100\n{candidate.reason}\nПримеры публикаций:\n{examples}")
        await self.bot.send_message(self.owner_id, text[:4000], reply_markup=candidate_keyboard(channel.id))
        self.repo.mark_candidate_notified(channel.id)

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
