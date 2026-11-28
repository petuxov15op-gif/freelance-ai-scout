from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from telethon import types
from telethon.errors import FloodWaitError, RPCError

from .ai import draft_needs_refresh
from .core import (announcement_parts, beginner_ai_role, borderline_it_order, clear_direct_it_request, contact_options, employment_type, explicit_ai_coding, explicit_it_task, high_risk_role,
                   kwork_excluded, kwork_project_url, office_required,
                   likely_job, mentions_platform, ongoing_role, potential_job, similar)
from .models import Channel, Message
from .outreach import OutreachService
from .repository import Repository
from .telegram import chat_search_term

log = logging.getLogger(__name__)

JOB_STATUS_LABELS = {
    "interesting": "⭐ Интересно",
    "irrelevant": "❌ Не подходит",
    "applied": "📨 Откликнулся",
    "won": "🏆 Получил заказ",
    "replied": "💬 Заказчик ответил",
    "negotiating": "🤝 Обсуждаем условия",
    "rejected": "🚫 Отказ",
    "no_reply": "⌛ Не ответили",
}

EMPLOYMENT_LABELS = {"one_time_project": "Разовый заказ", "recurring_project": "Регулярный проект",
                     "part_time_role": "Частичная занятость", "full_time_role": "Полная занятость"}
CONTACT_LABELS = {"direct_free": "Бесплатный", "platform_free": "Бесплатный",
                  "paid": "Платный", "unknown": "Не подтверждено", "none": "Не подтверждено"}
PRIORITY_CHANNEL = "program_job"
FALLBACK_DRAFT = "Здравствуйте! Пришлите, пожалуйста, материалы по задаче. Посмотрю и скажу, сколько займёт работа."

DISCOVERY_QUERIES = (
    "заказ сделать сайт", "заказ на лендинг", "фриланс разработка сайта",
    "нужен бот на заказ", "заказ на Telegram бота", "разовый заказ Python",
    "доработать сайт заказ", "верстка сайта заказ", "заказ на автоматизацию",
    "интеграция API заказ", "нужен скрипт фриланс", "заказы программистам",
)
DISCOVERY_GROUPS = {
    "development": DISCOVERY_QUERIES,
    "telegram_automation": ("заказ на Telegram бота", "автоматизировать заявки заказ",
                            "интеграция CRM фриланс", "автоматизация таблиц заказ"),
    "project_work": ("нужен подрядчик на сайт", "разовый IT заказ",
                     "фриланс веб разработка", "проектная разработка Codex"),
    "english": ("freelance web project", "telegram bot freelance", "api integration project"),
    "vibe_work": ("вайбкодер проект", "вайбкодинг подработка", "разработка через Cursor подработка",
                  "разработка с Codex проект", "AI разработчик без опыта Codex",
                  "вайбкодер неполный день", "Claude Code подработка"),
}
DEVELOPMENT_TASK = re.compile(
    r"\b(?:разработ|программ|сайт|лендинг|верстк|бот\b|автоматизац|интеграц|парсинг|"
    r"скрипт|приложен|crm\b|api\b|python\b|frontend\b|backend\b|wordpress\b|tilda\b|"
    r"вайбкод|vibecod|codex\b|cursor\b|ai\s+специалист|ai\s+developer)",
    re.IGNORECASE,
)


def discovery_fit(text: str) -> bool:
    if not likely_job(text) or not (DEVELOPMENT_TASK.search(text) or borderline_it_order(text)):
        return False
    if ongoing_role(text) and not explicit_ai_coding(text):
        return False
    if re.search(r"\b(?:дизайн\w*|дизайнер\w*|логотип\w*|баннер\w*|иллюстрац\w*)\b", text, re.I):
        if not re.search(r"\b(?:разработ\w*|верст\w*|код\w*|бот\w*|автоматизац\w*|интеграц\w*)\b", text, re.I):
            return False
    return True
PAUSED_PLATFORM_SOURCES = {
    "design_ai_jobs", "job_developer", "zakazy_it", "freelance_dev_work",
    "freelancers_kwork", "freelance_zakazy", "job_programming_apps_chatgpt_n8n",
    "remotefreelancejob", "workmasterit", "vibejobsru", "taylor_jobs", "horseatwork",
    "jobscode", "mobiledevjobs", "development_vakans",
}


def job_text(job, analysis, link: str | None) -> str:
    result = analysis.result
    requirements = ", ".join(result["requirements"][:4]) or "не указаны"
    status = JOB_STATUS_LABELS.get(job.status)
    return (f"{status + ' · ' if status else ''}Заказ #{job.id}: {result['title']}\n"
            f"Рейтинг: {job.score}/100 · {result['category']}\n"
            f"Занятость: {EMPLOYMENT_LABELS.get(job.employment_type, job.employment_type)}\n"
            f"Способ связи: {job.contact_method}\n"
            f"Отклик: {CONTACT_LABELS.get(job.contact_status, 'Не подтверждено')}\n"
            f"{('Контакт: ' + job.contact_url + chr(10)) if job.contact_url and job.contact_status in {'direct_free', 'platform_free'} else ''}"
            f"Бюджет: {result['budget'] or 'не указан'} {result['currency'] or ''}\n"
            f"Требования: {requirements}\n"
            f"Почему подходит: {result['score_reason'][:500]}\n"
            f"{('Моя сумма: ' + job.amount + chr(10)) if job.amount else ''}"
            f"{('Заметка: ' + job.note[:500] + chr(10)) if job.note else ''}"
            f"Источник: {link or 'ссылка недоступна'}")


class ScoutService:
    def __init__(self, repo: Repository, source, ai, bot: Bot, owner_id: int,
                 portfolio_urls: tuple[str, ...] = ()):
        self.repo, self.source, self.ai, self.bot, self.owner_id = repo, source, ai, bot, owner_id
        self.outreach = OutreachService(repo, source, bot, owner_id, portfolio_urls)
        self.monitor_lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.last_monitor_started: datetime | None = None
        self.last_monitor_finished: datetime | None = None
        self.last_monitor_errors: list[str] = []
        self.repo.seed_search_queries(DISCOVERY_GROUPS)

    def platforms_enabled(self) -> bool:
        return self.repo.setting("platform_sources_enabled", "false") == "true"

    @staticmethod
    def platform_source(channel: Channel | None) -> bool:
        return bool(channel) and (
            (channel.username or "").lower() in PAUSED_PLATFORM_SOURCES or mentions_platform(
                " ".join((channel.username or "", channel.title or "", channel.description or ""))))

    def source_paused(self, channel: Channel | None) -> bool:
        return not self.platforms_enabled() and self.platform_source(channel)

    @staticmethod
    def kwork_source(channel: Channel | None) -> bool:
        if not channel:
            return False
        name = " ".join((channel.username or "", channel.title or "")).lower()
        return "kwork" in name or "кворк" in name

    @classmethod
    def kwork_post(cls, channel: Channel | None, message) -> bool:
        if not cls.kwork_source(channel):
            return False
        links = (message.metadata_json or {}).get("outbound_links")
        return bool(kwork_project_url(links) or re.search(r"\bkwork(?:\.ru)?\b|кворк", message.text, re.I))

    async def set_platforms_enabled(self, enabled: bool) -> None:
        if enabled and not self.platforms_enabled():
            async with self.monitor_lock:
                for channel in self.repo.list_channels("approved"):
                    if self.platform_source(channel):
                        latest = await self.source.latest_message_id(channel)
                        self.repo.advance_cursor(channel.id, latest)
                self.repo.set_setting("platform_sources_enabled", "true")
        else:
            self.repo.set_setting("platform_sources_enabled", "true" if enabled else "false")

    async def add_channel(self, raw: str) -> Channel | None:
        from .telegram import parse_public_channel
        username = parse_public_channel(raw)
        entity = await self.source.resolve(username)
        try:
            inspection = await self.source.inspect(entity)
            examples, active, ad_ratio = inspection
        except Exception:
            log.exception("История канала %s недоступна; ручное добавление продолжено", username)
            inspection = None
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
        usefulness = max(0, min(100, len(verified) * 25 + (10 if active else 0)
                                 + round(20 * getattr(inspection, "contact_ratio", 0))
                                 - round(30 * ad_ratio) - round(20 * getattr(inspection, "repeat_ratio", 0))))
        reason = f"OpenRouter подтвердил {len(verified)} из {len(examples)} проверенных примеров; решение за вами"
        if not active:
            reason += "; канал неактивен более 30 дней"
        if ad_ratio > .5:
            reason += "; много рекламы"
        if getattr(inspection, "repeat_ratio", 0) > .5:
            reason += "; много повторов"
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
            channels = [channel for status in ("approved", "paused", "pending", "rejected", "blocked")
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
            seen_chat_terms: set[str] = set()

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
            enabled_groups = {group for group in DISCOVERY_GROUPS
                              if self.repo.setting("search_group_" + group, "true") == "true"}
            current_phrases = {phrase for group in enabled_groups for phrase in DISCOVERY_GROUPS[group]}
            query_rows = [row for row in self.repo.search_queries(enabled_groups) if row.phrase in current_phrases]
            # Alternate the existing and vibe-work tracks while keeping all enabled phrases eligible.
            def ranked_phrases(rows):
                ranked = sorted(rows, key=lambda row: (row.new_sources + row.suitable_jobs * 2 + row.applications * 3 + row.replies * 4) / (row.searches + 1), reverse=True)
                explored = sorted(rows, key=lambda row: row.searches)
                result = []
                for position in range(len(rows)):
                    pool = explored if position % 5 == 4 else ranked
                    row = next((item for item in pool if item.phrase not in result), None)
                    if row:
                        result.append(row.phrase)
                return result

            tracks = {
                "regular": ranked_phrases([row for row in query_rows if row.group != "vibe_work"]),
                "vibe": ranked_phrases([row for row in query_rows if row.group == "vibe_work"]),
            }
            positions = {name: int(self.repo.setting("discovery_" + name + "_index", "0")) % len(phrases)
                         for name, phrases in tracks.items() if phrases}
            remaining = {name: len(phrases) for name, phrases in tracks.items()}
            turn = self.repo.setting("discovery_next_track", "regular")
            while sum(remaining.values()):
                track = turn if remaining.get(turn) else ("vibe" if turn == "regular" else "regular")
                index = positions[track]
                query = tracks[track][index]

                def advance() -> None:
                    nonlocal turn
                    positions[track] = (index + 1) % len(tracks[track])
                    remaining[track] -= 1
                    turn = "vibe" if track == "regular" else "regular"
                    self.repo.set_setting("discovery_" + track + "_index", str(positions[track]))
                    self.repo.set_setting("discovery_next_track", turn)

                try:
                    search_error = None
                    try:
                        quota = await self.source.post_search_quota(query)
                    except FloodWaitError:
                        raise
                    except RPCError as exc:
                        quota = None
                        search_error = type(exc).__name__
                    if quota and not quota.remains and not quota.query_is_free:
                        wait_until = (datetime.fromtimestamp(quota.wait_till, timezone.utc) if quota.wait_till
                                      else datetime.now(timezone.utc) + timedelta(hours=1))
                        self.repo.set_setting("discovery_retry_at", wait_until.isoformat())
                        log.info("Бесплатный поиск Telegram исчерпан; повтор после %s", wait_until.isoformat())
                        return await finish("wait", wait_until)
                    found = []
                    if quota:
                        try:
                            found = await self.source.search_public_posts(query)
                        except FloodWaitError:
                            raise
                        except RPCError as exc:
                            # Public chat discovery is a separate API route.
                            search_error = type(exc).__name__
                            log.info("Поиск постов недоступен для %s: %s", query, type(exc).__name__)
                    chat_term = chat_search_term(query)
                    try:
                        chats = [] if chat_term in seen_chat_terms else await self.source.search_public_chats(chat_term, limit=30)
                        seen_chat_terms.add(chat_term)
                    except FloodWaitError:
                        raise
                    except Exception as exc:
                        errors += 1
                        search_error = type(exc).__name__
                        log.exception("Поиск публичных чатов по запросу %s не удался", query)
                        chats = []
                    advance()
                    self.repo.record_search(query, searches=1, success=True, error=search_error)
                except FloodWaitError as exc:
                    wait_until = datetime.now(timezone.utc) + timedelta(seconds=max(exc.seconds, 60))
                    self.repo.set_setting("discovery_retry_at", wait_until.isoformat())
                    self.repo.record_search(query, error=f"FloodWait {exc.seconds}s")
                    log.warning("Telegram ограничил поиск на %s секунд", exc.seconds)
                    return await finish("wait", wait_until)
                except (ConnectionError, OSError):
                    errors += 1
                    self.repo.record_search(query, error="Telegram connection error")
                    log.exception("Соединение с Telegram потеряно во время поиска")
                    return await finish("error", None)
                except Exception:
                    errors += 1
                    self.repo.record_search(query, error="Telegram search error")
                    log.exception("Поиск публикаций по запросу %s не удался", query)
                    advance()
                    continue
                for entity, post in [*found, *((chat, None) for chat in chats)]:
                    if entity.id in known:
                        self.repo.record_search(query, duplicates=1)
                        continue
                    if not self.platforms_enabled() and mentions_platform(entity.title or ""):
                        continue
                    content = ""
                    if post:
                        if post.date < datetime.now(timezone.utc) - timedelta(days=3):
                            continue
                        content = next((text for text, _ in announcement_parts(post.raw_text or "") if discovery_fit(text)), "")
                        if not content:
                            continue
                    if post and any(similar(content, prior) for prior in seen_examples):
                        self.repo.record_search(query, duplicates=1)
                        continue
                    known.add(entity.id)
                    try:
                        inspection = await self.source.inspect(entity, max_age_days=3)
                        examples, active, ad_ratio = inspection
                        if not active or ad_ratio > .5:
                            continue
                        description = await self.source.description(entity)
                        if not self.platforms_enabled() and mentions_platform(description or ""):
                            continue
                        examples = [text for text in dict.fromkeys([content, *examples]) if discovery_fit(text)]
                        if examples and any(similar(examples[0], prior) for prior in seen_examples):
                            self.repo.record_search(query, duplicates=1)
                            continue
                        verified = []
                        for example in examples[:3]:
                            if (await self.ai.ask("candidate", example))["suitable"]:
                                verified.append(example)
                                break
                        if not verified:
                            continue
                        direct = contact_options(verified[0], source_username=entity.username)["status"] == "direct_free"
                        usefulness = max(0, min(100, 50 + (30 if direct else 0)
                                                 + round(20 * getattr(inspection, "contact_ratio", 0))
                                                 - round(30 * ad_ratio)
                                                 - round(20 * getattr(inspection, "repeat_ratio", 0))))
                        channel = self.repo.candidate(
                            entity.id, entity.username, entity.title, description, query,
                            usefulness,
                            (f"Недавние IT-заказы и AI-вакансии: {len(verified)}; чат активен за последние 3 дня; "
                             + ("есть прямой контакт заказчика" if direct else "прямой контакт не указан")
                             + (f"; повторы {getattr(inspection, 'repeat_ratio', 0):.0%}, реклама {ad_ratio:.0%}")), verified,
                        )
                        if channel:
                            self.repo.record_search(query, new_sources=1)
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

    async def monitor_kwork(self) -> None:
        await self.monitor(kwork_only=True)

    def channel_interval(self, row: dict, now: datetime | None = None) -> float:
        channel = row["channel"]
        if (channel.username or "").lower() == PRIORITY_CHANNEL:
            return .5
        if self.outreach.enabled() or self.kwork_source(channel) or row["positive_30d"] or row["saved"]:
            return 2
        if row["alerts"] > row["rejected"]:
            return 5
        now = now or datetime.now(timezone.utc)
        approved_at = channel.reviewed_at or channel.created_at
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
        return 30 if approved_at <= now - timedelta(days=7) else 10

    async def monitor_due(self) -> None:
        await self.monitor(due_only=True)

    async def monitor_priority(self, updated_channels: set[int] | None = None) -> None:
        if self.repo.setting("monitoring_paused", "false") == "true":
            return
        async with self.monitor_lock:
            updated_channels = updated_channels or set()
            for channel in self.repo.list_channels("approved"):
                urgent = (channel.username or "").lower() == PRIORITY_CHANNEL or channel.id in updated_channels
                if not urgent or self.source_paused(channel):
                    continue
                try:
                    async for item in self.source.messages(channel, self.repo.cursor(channel.id)):
                        self._save_item(channel, item)
                        await self.process_pending(channel_id=channel.id)
                    self.repo.mark_channel_checked(channel.id)
                    await self.process_pending(channel_id=channel.id)
                except Exception:
                    log.exception("Быстрая проверка канала %s прервана; повторим позже", channel.id)

    async def monitor(self, kwork_only: bool = False, due_only: bool = False) -> None:
        if self.repo.setting("monitoring_paused", "false") == "true":
            return
        async with self.monitor_lock:
            self.last_monitor_started = datetime.now(timezone.utc)
            self.last_monitor_errors = []
            try:
                channels = self.repo.list_channels("approved")
                channels = [channel for channel in channels if not self.source_paused(channel)]
                if kwork_only:
                    channels = [channel for channel in channels if self.kwork_source(channel)]
                elif due_only:
                    now = datetime.now(timezone.utc)
                    reports = {row["channel"].id: row for row in self.repo.channel_report()}
                    def due(channel):
                        row = reports[channel.id]
                        if (channel.username or "").lower() == PRIORITY_CHANNEL:
                            return False
                        checked = row["checked_at"]
                        if checked is None:
                            return True
                        checked = checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
                        return checked <= now - timedelta(minutes=self.channel_interval(row, now))
                    channels = [channel for channel in channels if due(channel)]
                    channels.sort(key=lambda channel: self.channel_interval(reports[channel.id], now))
                else:
                    channels.sort(key=lambda channel: (channel.username or "").lower() != PRIORITY_CHANNEL)
                log.info("Мониторинг начат: одобренных каналов %s", len(channels))
                for channel in channels:
                    if self.repo.setting("monitoring_paused", "false") == "true":
                        break
                    try:
                        async for item in self.source.messages(channel, self.repo.cursor(channel.id)):
                            self._save_item(channel, item)
                            await asyncio.sleep(.3)
                        self.repo.mark_channel_checked(channel.id)
                        if (channel.username or "").lower() == PRIORITY_CHANNEL:
                            await self.process_pending(channel_id=channel.id)
                        elif due_only:
                            await self.process_pending(channel_id=channel.id)
                    except Exception as exc:
                        self.last_monitor_errors.append(f"@{channel.username or channel.id}: {type(exc).__name__}")
                        log.exception("Мониторинг канала %s прерван, курсор сохранён", channel.title)
                if self.repo.setting("monitoring_paused", "false") != "true":
                    await self.process_pending()
                log.info("Мониторинг завершён: одобренных каналов %s", len(channels))
            except Exception as exc:
                self.last_monitor_errors.append(f"Общий сбой: {type(exc).__name__}")
                raise
            finally:
                self.last_monitor_finished = datetime.now(timezone.utc)

    async def set_channel_monitoring(self, channel_id: int, enabled: bool) -> bool:
        async with self.monitor_lock:
            channel = self.repo.get_channel(channel_id)
            if channel is None or channel.status != ("paused" if enabled else "approved"):
                return False
            latest = await self.source.latest_message_id(channel) if enabled else 0
            return self.repo.set_monitoring(channel_id, enabled, latest)

    def _save_item(self, channel: Channel, item, advance_cursor: bool = True) -> None:
        from .telegram import outbound_links
        links = outbound_links(item)
        link = (kwork_project_url(links) if self.kwork_source(channel) else None)
        link = link or (f"https://t.me/{channel.username}/{item.id}" if channel.username else None)
        sender = getattr(item, "sender", None)
        author = sender.username if isinstance(sender, types.User) else None
        contact = contact_options(item.raw_text or "", links, channel.username, author)
        self.repo.save_message(channel.id, item.id, item.raw_text or "", link, item.date,
                               {"has_media": bool(item.media), "forwarded": bool(item.fwd_from),
                                "outbound_links": links, "contact_url": contact.get("url"),
                                "author_username": author}, advance_cursor=advance_cursor)

    async def backfill_today(self) -> tuple[int, int, int, int, list[str]]:
        async with self.monitor_lock:
            self.last_monitor_started = datetime.now(timezone.utc)
            self.last_monitor_errors = []
            today = datetime.now(ZoneInfo("Asia/Yekaterinburg")).replace(
                hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            channels = [channel for channel in self.repo.list_channels("approved")
                        if not self.source_paused(channel)]
            before = self.repo.stats()["messages"]
            notifications_before = self.repo.sent_notification_count()
            scanned = 0
            try:
                for channel in channels:
                    try:
                        async for item in self.source.messages_since(channel, today):
                            self._save_item(channel, item, advance_cursor=False)
                            scanned += 1
                            await asyncio.sleep(.3)
                    except Exception as exc:
                        self.last_monitor_errors.append(f"@{channel.username or channel.id}: {type(exc).__name__}")
                        log.exception("Повторная проверка канала %s прервана", channel.title)
                new_messages = self.repo.stats()["messages"] - before
                await self.process_pending(limit=None)
                for job_id in self.repo.today_unsent_job_ids(today):
                    try:
                        await self.notify_job(job_id, "instant", allow_unconfirmed=True)
                    except Exception as exc:
                        self.last_monitor_errors.append(f"Уведомление: {type(exc).__name__}")
                        log.exception("Ручное уведомление заказа %s не удалось", job_id)
                sent = self.repo.sent_notification_count() - notifications_before
                return len(channels), scanned, new_messages, sent, self.last_monitor_errors.copy()
            finally:
                self.last_monitor_finished = datetime.now(timezone.utc)

    async def process_pending(self, limit: int | None = 500, channel_id: int | None = None) -> None:
        self.repo.recover_stale_notifications()
        pending = deque(self.repo.pending_messages(limit=limit, channel_id=channel_id))
        while pending:
            message = pending.popleft()
            try:
                channel = self.repo.get_channel(message.channel_id)
                if channel is None or channel.status != "approved":
                    continue
                if self.source_paused(channel):
                    self.repo.mark_message(message.id, "irrelevant", "source_paused")
                    continue
                if not message.part_number:
                    parts = announcement_parts(message.text, (message.metadata_json or {}).get("outbound_links"))
                    if len(parts) > 1:
                        pending.extendleft(reversed(self._split_post(message, channel, parts)))
                        continue
                employment = employment_type(message.text)
                contact = contact_options(message.text, (message.metadata_json or {}).get("outbound_links"),
                                          channel.username if channel else None,
                                          (message.metadata_json or {}).get("author_username"))
                reason = ("kwork_excluded" if self.kwork_post(channel, message) and kwork_excluded(message.text) else
                          "not_job" if not potential_job(message.text) else
                          "no_it_signal" if not (explicit_it_task(message.text) or borderline_it_order(message.text)) else
                          "requires_verified_expertise" if high_risk_role(message.text) else
                          "office_required" if office_required(message.text) else
                          "ongoing_without_ai_coding" if ongoing_role(message.text) and not explicit_ai_coding(message.text) else
                          "category_disabled" if self.repo.setting("employment_" + employment,
                              "false" if employment == "full_time_role" else "true") != "true"
                              and not (employment == "full_time_role" and beginner_ai_role(message.text)
                                       and self.repo.setting("employment_beginner_ai_role", "true") == "true") else None)
                if reason:
                    self.repo.mark_message(message.id, "irrelevant", reason)
                    continue
                existing = self.repo.matching_job(message)
                if existing and self.repo.analysis(existing.id):
                    self.repo.attach_job(message)
                    if (existing.applied_at is None and existing.status in {"new", "interesting"}
                            and self.outreach.target(channel, message, contact)
                            and not self.repo.has_outreach(existing.id)):
                        try:
                            redraft = await self.ai.ask("draft", message.text)
                            await self.outreach.apply(existing.id, channel, message, contact, redraft)
                        except Exception:
                            log.exception("Не удалось подготовить автоотклик по заказу %s", existing.id)
                            continue
                    if existing.contact_status not in {"direct_free", "platform_free"} and contact["status"] in {"direct_free", "platform_free"}:
                        self.repo.set_job_details(existing.id, employment, contact)
                        await self.notify_job(existing.id, "instant")
                    self.repo.mark_message(message.id, "linked")
                    continue
                priority = (channel.username or "").lower() == PRIORITY_CHANNEL
                direct_request = (priority and
                                  clear_direct_it_request(message.text, contact["status"], employment))
                if not direct_request:
                    pre = await self.ai.ask("kwork_prefilter" if self.kwork_post(channel, message) else "prefilter", message.text)
                    if not (pre["is_job"] and pre["codex_feasible"] and pre["is_it"]):
                        reason = "ai_not_job" if not pre["is_job"] else "ai_not_feasible" if not pre["codex_feasible"] else "ai_not_it"
                        self.repo.mark_message(message.id, "irrelevant", reason)
                        continue
                job, created = self.repo.attach_job(message)
                if not created and self.repo.analysis(job.id):
                    self.repo.mark_message(message.id, "linked")
                    continue
                auto_reply = self.outreach.target(channel, message, contact)
                prepare_draft = auto_reply or (priority and contact["status"] in {"direct_free", "platform_free"})
                if prepare_draft:
                    deep, draft = await asyncio.gather(self.ai.ask("deep", message.text),
                                                       self.ai.ask("draft", message.text), return_exceptions=True)
                    if isinstance(deep, Exception):
                        raise deep
                else:
                    deep, draft = await self.ai.ask("deep", message.text), None
                self.repo.save_analysis(job.id, "deep", self.ai.model, deep)
                if prepare_draft and isinstance(draft, dict) and isinstance(draft.get("draft"), str) and draft["draft"].strip():
                    self.repo.save_analysis(job.id, "draft_saved", "owner", draft)
                elif priority and prepare_draft:
                    log.warning("Отклик для заказа %s не подготовлен ИИ; используем короткий запасной вариант", job.id)
                    self.repo.save_analysis(job.id, "draft_saved", "owner", {"draft": FALLBACK_DRAFT})
                self.repo.set_job_details(job.id, employment, contact)
                if auto_reply and not isinstance(draft, Exception):
                    await self.outreach.apply(job.id, channel, message, contact, draft)
                candidate = self.repo.get_candidate(message.channel_id)
                if candidate and candidate.query:
                    self.repo.record_search(candidate.query, suitable_jobs=1)
                if not (auto_reply and isinstance(draft, Exception)):
                    self.repo.mark_message(message.id, "linked")
                await self.notify_job(job.id, "instant")
            except Exception as exc:
                self.last_monitor_errors.append(f"Обработка объявления: {type(exc).__name__}")
                log.exception("Обработка объявления %s прервана; повторим позже", message.id)
        for job in self.repo.pending_instant_jobs(0, channel_id=channel_id):
            if self.repo.analysis(job.id):
                try:
                    await self.notify_job(job.id, "instant")
                except Exception as exc:
                    self.last_monitor_errors.append(f"Уведомление: {type(exc).__name__}")
                    log.exception("Повторное уведомление заказа %s не удалось", job.id)

    def _split_post(self, message: Message, channel: Channel, parts: list[tuple[str, list[dict]]]) -> list[Message]:
        source_link = f"https://t.me/{channel.username}/{message.telegram_message_id}" if channel.username else None
        rows = []
        for text, links in parts:
            project_link = None
            if self.kwork_source(channel):
                plain_links = [{"url": match.rstrip(".,;)")} for match in re.findall(r"https?://[^\s<>]+", text)]
                project_link = kwork_project_url([*links, *plain_links])
            contact = contact_options(text, links, channel.username)
            rows.append((text, project_link or source_link,
                        {"outbound_links": links, "contact_url": contact.get("url")}))
        return self.repo.split_message(message.id, rows)

    async def notify_job(self, job_id: int, kind: str, allow_unconfirmed: bool = False) -> None:
        from .cards import NO_PREVIEW, card_keyboard, card_text, source_links
        message = self.repo.job_message(job_id)
        channel = self.repo.job_channel(job_id)
        job = self.repo.job(job_id)
        if not job or message is None or channel is None or channel.status != "approved" or self.source_paused(channel):
            return
        kwork = self.kwork_post(channel, message)
        entry_role_form = (beginner_ai_role(message.text)
                           and job.contact_status == "unknown" and bool(job.contact_url))
        if not allow_unconfirmed and job.contact_status not in {"direct_free", "platform_free"} and not kwork and not entry_role_form:
            return
        if kwork and not allow_unconfirmed:
            started = datetime.fromisoformat(self.repo.setting("kwork_auto_start_at"))
            published = message.published_at.replace(tzinfo=timezone.utc) if message.published_at.tzinfo is None else message.published_at
            if published < max(started, datetime.now(timezone.utc) - timedelta(minutes=30)):
                return
        if not self.repo.claim_notification(job_id, kind):
            return
        try:
            saved = self.repo.analysis(job_id, "draft_saved") if (channel.username or "").lower() == PRIORITY_CHANNEL else None
            quick_draft = saved.result.get("draft") if saved and not draft_needs_refresh(saved.result) else None
            link, channel_link, kwork_link = source_links(self.repo, job_id)
            sent = await self.bot.send_message(
                self.owner_id, card_text(self.repo, job_id, quick_draft=quick_draft), parse_mode="HTML", link_preview_options=NO_PREVIEW,
                reply_markup=card_keyboard(job_id, link, job.status, job.saved,
                                           quick_draft=quick_draft, channel_link=channel_link, kwork_link=kwork_link))
            self.repo.finish_notification(job_id, kind, sent.message_id)
        except Exception:
            self.repo.release_notification(job_id, kind)
            raise
