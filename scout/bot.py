from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from .service import CONTACT_LABELS, DISCOVERY_GROUPS, EMPLOYMENT_LABELS, JOB_STATUS_LABELS, job_text
from .telegram import parse_public_channel

log = logging.getLogger(__name__)

MENU_SECTIONS = {
    "📋 Заказы": ("latest", "top"),
    "📡 Каналы": ("sources", "pending", "discover", "insights", "platforms"),
    "📊 Бот": ("status", "check", "today", "stats", "analytics", "settings"),
}
MENU_LABELS = {
    "latest": "📋 Последние",
    "top": "⭐ Лучшие",
    "sources": "📡 Источники",
    "pending": "🕓 Кандидаты",
    "discover": "🔎 Найти чаты сейчас",
    "platforms": "🏪 Площадки",
    "insights": "📡 Эффективность поиска",
    "status": "📊 Статус",
    "check": "🩺 Проверить",
    "today": "🔄 Проверить сегодня",
    "stats": "📈 Статистика",
    "analytics": "📊 Отклики",
    "settings": "⚙️ Настройки",
}
ADD_CHANNEL_LABEL = "➕ Добавить канал"
BACK_LABEL = "← Назад"

# Keep accepting taps from keyboards sent before the menu update.
MENU_ACTIONS = {
    "📡 Источники": "sources",
    "🕓 Кандидаты": "pending",
    "🔎 Найти чаты сейчас": "discover",
    "🏪 Площадки": "platforms",
    "➕ Добавить канал": "add",
    "📋 Заказы": "latest",
    "⭐ Лучшие": "top",
    "📊 Статус": "status",
    "🩺 Проверить": "check",
    "🔄 Проверить сегодня": "today",
    "📈 Статистика": "stats",
    "📊 Отклики": "analytics",
    "📡 Эффективность поиска": "insights",
    "⚙️ Настройки": "settings",
}


def main_menu() -> ReplyKeyboardMarkup:
    labels = ["📋 Заказы", "📡 Каналы", ADD_CHANNEL_LABEL, "📊 Бот"]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in labels[i:i + 2]]
                  for i in range(0, len(labels), 2)],
        resize_keyboard=True, is_persistent=True,
    )


def section_menu(section: str) -> ReplyKeyboardMarkup:
    labels = [MENU_LABELS[action] for action in MENU_SECTIONS[section]]
    rows = [[KeyboardButton(text=label) for label in labels[i:i + 2]]
            for i in range(0, len(labels), 2)]
    rows.append([KeyboardButton(text=BACK_LABEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in (
        ("menu", "Показать кнопки"), ("status", "Состояние бота"),
        ("check", "Проверить работу бота"),
        ("today", "Проверить публикации за сегодня"),
        ("sources", "Отслеживаемые каналы"),
        ("pending", "Каналы на подтверждении"), ("add", "Добавить канал"),
        ("latest", "Последние заказы"), ("top", "Лучшие заказы"),
        ("stats", "Статистика"), ("settings", "Настройки"),
        ("analytics", "Аналитика откликов"),
    )]


def parse_channel_list(text: str) -> list[str]:
    parts = text.replace(",", " ").split()
    if any(not part.startswith(("@", "https://t.me/")) for part in parts):
        raise ValueError("Укажите каналы в виде @имя или https://t.me/имя")
    channels = [parse_public_channel(part) for part in parts]
    if not channels or len(channels) > 20:
        raise ValueError("Укажите от 1 до 20 публичных каналов через пробел")
    return list(dict.fromkeys(channels))


def candidate_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить", callback_data=f"channel:approved:{channel_id}"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"channel:rejected:{channel_id}")],
        [InlineKeyboardButton(text="Заблокировать", callback_data=f"channel:blocked:{channel_id}"),
         InlineKeyboardButton(text="Посмотреть примеры", callback_data=f"examples:{channel_id}")],
    ])


def candidate_digest_keyboard(channel_ids: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{number} Добавить", callback_data=f"channel:approved:{channel_id}"),
         InlineKeyboardButton(text=f"{number} Отклонить", callback_data=f"channel:rejected:{channel_id}")]
        for number, channel_id in enumerate(channel_ids, 1)
    ])


def job_keyboard(job_id: int, link: str | None, status: str = "new", editing: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text="Открыть объявление", url=link)])
    rows.append(
        [InlineKeyboardButton(text="Разобрать заказ", callback_data=f"plan:{job_id}"),
         InlineKeyboardButton(text="Подготовить отклик", callback_data=f"draft:{job_id}")]
    )
    if status != "new" and not editing:
        rows.append([InlineKeyboardButton(text="Изменить решение", callback_data=f"feedback:change:{job_id}")])
    else:
        rows.extend([
            [InlineKeyboardButton(text="Интересно", callback_data=f"feedback:interesting:{job_id}"),
             InlineKeyboardButton(text="Не подходит", callback_data=f"feedback:irrelevant:{job_id}")],
            [InlineKeyboardButton(text="Откликнулся", callback_data=f"feedback:applied:{job_id}"),
             InlineKeyboardButton(text="Заказчик ответил", callback_data=f"feedback:replied:{job_id}")],
            [InlineKeyboardButton(text="Обсуждаем", callback_data=f"feedback:negotiating:{job_id}"),
             InlineKeyboardButton(text="Получил заказ", callback_data=f"feedback:won:{job_id}")],
            [InlineKeyboardButton(text="Отказ", callback_data=f"feedback:rejected:{job_id}"),
             InlineKeyboardButton(text="Не ответили", callback_data=f"feedback:no_reply:{job_id}")],
        ])
        if status != "new":
            rows.append([InlineKeyboardButton(text="Снять отметку", callback_data=f"feedback:new:{job_id}")])
    rows.append([InlineKeyboardButton(text="Заметка", callback_data=f"field:note:{job_id}"),
                 InlineKeyboardButton(text="Сумма", callback_data=f"field:amount:{job_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def health_text(service) -> str:
    lines = ["Проверка бота", "Бот отвечает: да"]
    problems = []
    try:
        stats = service.repo.stats()
        activity = service.repo.health_activity()
        pending = len(service.repo.pending_messages())
        lines.append(f"База данных: работает; одобренных каналов: {stats['approved']}; объявлений в очереди: {pending}")
        lines.append(f"За 24 часа: прочитано сообщений {activity['messages']}, отправлено уведомлений {activity['notifications']}")
        if activity["last_notification"]:
            sent = activity["last_notification"].astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m %H:%M")
            lines.append(f"Последнее уведомление: {sent} (Екб)")
        if not stats["approved"]:
            problems.append("Нет одобренных каналов для мониторинга")
    except Exception as exc:
        log.exception("Проверка базы данных")
        problems.append(f"База данных: ошибка {type(exc).__name__}")

    try:
        connected = service.source.client.is_connected()
        lines.append(f"Чтение Telegram-каналов: {'подключено' if connected else 'отключено'}")
        if not connected:
            problems.append("Сессия Telegram для чтения каналов отключена")
    except Exception as exc:
        log.exception("Проверка сессии Telegram")
        problems.append(f"Сессия Telegram: ошибка {type(exc).__name__}")

    started = service.last_monitor_started
    if started is None:
        lines.append("Мониторинг: после запуска ещё не выполнялся")
        problems.append("Мониторинг ещё не запускался")
    else:
        local_time = started.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m %H:%M")
        running = service.monitor_lock.locked()
        lines.append(f"Мониторинг: {'идёт сейчас' if running else 'последний запуск'} {local_time} (Екб)")
        if not running and (datetime.now(timezone.utc) - started).total_seconds() > 25 * 60:
            problems.append("Мониторинг не запускался более 25 минут")
        if service.last_monitor_errors:
            errors = service.last_monitor_errors
            problems.append(f"Ошибки последнего прохода ({len(errors)}): " + "; ".join(errors[:3]))

    try:
        ai_ok, ai_detail = await service.ai.check_health()
        label = "OpenRouter" if getattr(service.ai, "provider", "openrouter") == "openrouter" else "Сторонний API"
        lines.append(label + ": " + ai_detail)
        if not ai_ok:
            problems.append("Проверка ИИ не прошла")
        if getattr(service.ai, "provider", "openrouter") == "openrouter":
            lines.append(f"Расход по учёту бота сегодня: ${service.repo.cost_today():.4f}")
    except Exception as exc:
        log.exception("Проверка ИИ")
        problems.append(f"Проверка ИИ: ошибка {type(exc).__name__}")

    lines.append("Итог: " + ("обнаружены проблемы" if problems else "основные проверки прошли"))
    lines.extend("• " + problem for problem in problems)
    return "\n".join(lines)


class OwnerMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id != self.owner_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Доступ запрещён")
            return None
        return await handler(event, data)


def create_router(service) -> Router:
    router = Router()
    router.message.middleware(OwnerMiddleware(service.owner_id))
    router.callback_query.middleware(OwnerMiddleware(service.owner_id))
    repo = service.repo
    pending_input: dict[int, tuple[str, int]] = {}
    def settings_text() -> str:
        return "Уведомления: подходящие IT-варианты с подтверждённым бесплатным откликом. Выбери категории и группы поиска:"

    def settings_keyboard() -> InlineKeyboardMarkup:
        rows = []
        for key, label in EMPLOYMENT_LABELS.items():
            enabled = repo.setting("employment_" + key, "false" if key == "full_time_role" else "true") == "true"
            rows.append([InlineKeyboardButton(text=("✅ " if enabled else "⬜ ") + label,
                                              callback_data=f"toggle:employment:{key}")])
        for key, label in (("development", "Разработка"), ("telegram_automation", "Telegram и автоматизация"),
                           ("project_work", "Проектная работа"), ("english", "English")):
            enabled = repo.setting("search_group_" + key, "true") == "true"
            rows.append([InlineKeyboardButton(text=("✅ " if enabled else "⬜ ") + "Поиск: " + label,
                                              callback_data=f"toggle:search:{key}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def analytics_text(days: int | None) -> str:
        values = repo.analytics(days)
        applied, replied, won = values["applied"], values["replied"], values["won"]
        response_rate = f"{values['replied_from_applied'] / applied:.0%}" if applied else "Недостаточно данных"
        win_rate = f"{values['won_from_applied'] / applied:.0%}" if applied else "Недостаточно данных"
        return (f"Аналитика: {days or 'всё время'} дн.\n"
                f"Публикаций найдено: {values['found']}\n"
                f"Уникальных заказов: {values['jobs']}\n"
                f"Подходящих с бесплатным откликом: {values['unique']}\n"
                f"Откликнулся: {applied} · Ответов: {replied} · Получил заказ: {won}\n"
                f"Доля ответов: {response_rate} · Доля выигранных: {win_rate}")

    def analytics_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="7 дней", callback_data="analytics:7"),
            InlineKeyboardButton(text="30 дней", callback_data="analytics:30"),
            InlineKeyboardButton(text="Всё время", callback_data="analytics:all")]])

    async def switch_menu(message: Message, keyboard: ReplyKeyboardMarkup, title: str) -> None:
        await message.answer(title, reply_markup=keyboard, disable_notification=True)
        try:
            await message.delete()
        except Exception:
            log.exception("Не удалось удалить нажатие кнопки меню")

    async def add_channels(message: Message, raw: str) -> None:
        try:
            usernames = parse_channel_list(raw)
        except ValueError as exc:
            await message.answer(f"{exc}. Пример: @channel_name")
            return
        results = []
        for username in usernames:
            try:
                channel = await service.add_channel("@" + username)
                results.append(f"@{username}: проверьте карточку и нажмите «Добавить»" if channel
                               else f"@{username}: канал уже известен")
            except ValueError as exc:
                results.append(f"@{username}: {exc}")
            except Exception as exc:
                log.exception("Ручное добавление канала %s", username)
                results.append(f"@{username}: не удалось добавить ({type(exc).__name__})")
        await message.answer("\n".join(results)[:4000])

    @router.message(Command("start"))
    async def start(message: Message):
        await message.answer("Выберите действие на кнопках ниже.", reply_markup=main_menu())

    @router.message(Command("menu"))
    async def menu(message: Message):
        await message.answer("Главное меню", reply_markup=main_menu())

    @router.message(Command("status"))
    async def status(message: Message):
        stats = repo.stats()
        telegram_status = "подключён" if service.source.client.is_connected() else "отключён"
        provider_status = (f"OpenRouter: {service.ai.last_status}, модель {service.ai.model}, учтено сегодня ${repo.cost_today():.4f}"
                           if service.ai.provider == "openrouter" else
                           f"Сторонний API: {service.ai.last_status}, модель {service.ai.model}; остаток и расходы смотри у продавца")
        await message.answer(f"Источники: {stats['approved']} · кандидаты: {stats['pending']}\n"
                             "Поиск новых чатов: по бесплатному лимиту Telegram\nМониторинг: каждые 10 минут\n"
                             "Присылаю: разовые IT-заказы и IT-работу/подработку с явным вайбкодингом\n"
                             "Уведомления: без порога рейтинга\n"
                             f"{provider_status}\n"
                             f"Telegram: {telegram_status}")

    @router.message(Command("check"))
    async def check(message: Message):
        await message.answer(await health_text(service))

    @router.message(Command("today"))
    async def today(message: Message):
        if service.monitor_lock.locked():
            await message.answer("Проверка уже идёт. Попробуй ещё раз после её завершения.")
            return
        await message.answer("Проверяю все публикации за сегодня в одобренных каналах. Подходящие предложения пришлю отдельно.")
        try:
            channels, scanned, new, errors = await service.backfill_today()
        except Exception:
            log.exception("Ручная проверка публикаций за сегодня")
            await message.answer("Проверка прервалась. Подробности записаны в журнал бота.")
            return
        await message.answer(f"Проверка за сегодня завершена: каналов {channels}, публикаций {scanned}, "
                             f"новых для базы {new}."
                             + (f" Ошибки в {len(errors)} случаях; попробуй позже ещё раз." if errors else ""))

    @router.message(Command("sources"))
    async def sources(message: Message):
        channels = repo.list_channels("approved")
        if not channels:
            await message.answer("Одобренных каналов нет")
            return
        parts = (message.text or "").split()
        try:
            page = max(1, int(parts[1])) if len(parts) > 1 else 1
        except ValueError:
            page = 1
        selected = channels[(page - 1) * 50:page * 50]
        await message.answer((f"Источники: страница {page} из {(len(channels) + 49) // 50}\n" +
                              "\n".join(f"{c.id}. {c.title} · @{c.username}"
                                        + (" · 💤 пауза" if service.source_paused(c) else "") for c in selected))[:4000]
                             if selected else "На этой странице нет каналов")

    @router.message(Command("platforms"))
    async def platforms(message: Message):
        channels = [channel for channel in repo.list_channels("approved")
                    if service.platform_source(channel)]
        enabled = service.platforms_enabled()
        label = "⏸ Выключить площадки" if enabled else "▶️ Включить площадки"
        text = (f"Площадки: {'включены' if enabled else 'на паузе'}\n"
                f"Объявления без прямого Telegram-контакта {'проверяются' if enabled else 'не присылаются'}.\n"
                f"Источники без прямого отклика ({len(channels)}):\n"
                + ("\n".join(f"• @{c.username} — {c.title}" for c in channels) or "Пока нет"))
        await message.answer(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"platforms:{'off' if enabled else 'on'}")]]))

    @router.message(Command("pending"))
    async def pending(message: Message):
        channels = []
        for channel in repo.list_channels("pending"):
            candidate = repo.get_candidate(channel.id)
            if not service.source_paused(channel):
                channels.append(channel)
        if not channels:
            await message.answer("Нет каналов на подтверждении")
            return
        parts = (message.text or "").split()
        try:
            page = max(1, int(parts[1])) if len(parts) > 1 else 1
        except ValueError:
            page = 1
        selected = channels[(page - 1) * 20:page * 20]
        if not selected:
            await message.answer("На этой странице нет каналов")
            return
        await message.answer(f"Кандидаты: страница {page} из {(len(channels) + 19) // 20}. Следующая: /pending {page + 1}")
        for channel in selected:
            candidate = repo.get_candidate(channel.id)
            if candidate and not candidate.owner_notified:
                await service.notify_candidate(channel)
            else:
                await message.answer(f"{channel.title} · @{channel.username}", reply_markup=candidate_keyboard(channel.id))

    @router.message(Command("add"))
    async def add(message: Message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Пример: /add @channel_name или /add @first @second")
            return
        await add_channels(message, parts[1])

    @router.message(Command("latest"))
    async def latest(message: Message):
        jobs = repo.jobs()
        await message.answer("\n".join(f"{JOB_STATUS_LABELS.get(j.status, '🆕 Новый')} · #{j.id} · {j.score} · {j.title}" for j in jobs)[:4000] or "Заказов пока нет")

    @router.message(Command("top"))
    async def top(message: Message):
        jobs = sorted(repo.jobs(60, 50), key=lambda item: item.score or 0, reverse=True)[:10]
        await message.answer("\n".join(f"#{j.id} · {j.score} · {j.title}" for j in jobs)[:4000] or "Подходящих заказов пока нет")

    @router.message(Command("stats"))
    async def stats(message: Message):
        values = repo.stats()
        await message.answer("\n".join(f"{key}: {value}" for key, value in values.items()))

    @router.message(Command("analytics"))
    async def analytics(message: Message):
        await message.answer(analytics_text(30), reply_markup=analytics_keyboard())

    @router.message(Command("insights"))
    async def insights(message: Message):
        queries = sorted(repo.search_queries(), key=lambda item: (item.suitable_jobs, item.new_sources, item.applications), reverse=True)[:5]
        lines = ["Эффективность поиска:"]
        lines.extend(f"• {q.phrase}: поисков {q.searches}, источников {q.new_sources}, подходящих {q.suitable_jobs}, дублей {q.duplicates}, откликов {q.applications}, ответов {q.replies}" for q in queries)
        lines.append("Полезные каналы:")
        lines.extend(f"• @{name}: {count} заказов, {apps} откликов, {replies} ответов, {wins} побед"
                     for name, count, apps, replies, wins in repo.top_channels())
        rejected = repo.rejection_counts()
        if rejected:
            lines.append("Причины отклонения: " + ", ".join(f"{reason} — {count}" for reason, count in rejected))
        retry = repo.setting("discovery_retry_at")
        if retry:
            try:
                until = datetime.fromisoformat(retry)
                if until > datetime.now(timezone.utc):
                    lines.append(f"Поиск ждёт лимит Telegram до {until.astimezone(ZoneInfo('Asia/Yekaterinburg')).strftime('%d.%m %H:%M')} (Екб)")
            except ValueError:
                pass
        errors = [(q.phrase, q.last_error) for q in repo.search_queries() if q.last_error]
        if errors:
            lines.append("Последняя ошибка: " + errors[-1][0] + " — " + errors[-1][1])
        await message.answer("\n".join(lines)[:4000])

    @router.message(Command("settings"))
    async def settings(message: Message):
        await message.answer(settings_text(), reply_markup=settings_keyboard())

    @router.message(Command("note"))
    async def note(message: Message):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit() or not repo.set_job_note(int(parts[1]), parts[2]):
            await message.answer("Пример: /note 12 Созвон в пятницу")
            return
        await message.answer("Заметка сохранена")

    @router.message(Command("amount"))
    async def amount(message: Message):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit() or not repo.set_job_amount(int(parts[1]), parts[2]):
            await message.answer("Пример: /amount 12 25000 ₽")
            return
        await message.answer("Сумма сохранена")

    @router.message(Command("discover"))
    async def discover(message: Message):
        if service.discovery_lock.locked():
            await message.answer("Поиск уже идёт. Подожди итоговое сообщение.")
            return
        await message.answer("Запускаю поиск IT-чатов. Когда проверка закончится, пришлю итог и карточки подходящих чатов.")
        try:
            status, found, errors, retry = await service.discover()
        except Exception:
            log.exception("Ручной поиск чатов")
            await message.answer("Поиск завершился с ошибкой. Подробности записаны в журнал бота.")
            return
        if status == "running":
            await message.answer("Поиск уже идёт. Подожди итоговое сообщение.")
        elif status == "wait":
            time = retry.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m %H:%M") if retry else "позже"
            await message.answer(f"Бесплатный поиск Telegram пока недоступен. Повторю после {time} (Екб)."
                                 + (f" Новых чатов на рассмотрение: {found}." if found else ""))
        elif status == "error":
            await message.answer("Поиск остановлен: нет соединения с Telegram для чтения каналов. "
                                 "Бот попробует подключиться заново при следующей проверке.")
        else:
            await message.answer(f"Поиск завершён. Новых чатов на рассмотрение: {found}."
                                 + (f" Ошибок при проверке: {errors}." if errors else ""))

    @router.message(F.text.in_({*MENU_SECTIONS, ADD_CHANNEL_LABEL, BACK_LABEL,
                                *MENU_LABELS.values(), *MENU_ACTIONS}))
    async def menu_action(message: Message):
        if message.text in MENU_SECTIONS:
            await switch_menu(message, section_menu(message.text), "Выбери действие")
            return
        if message.text == BACK_LABEL:
            await switch_menu(message, main_menu(), "Главное меню")
            return
        if message.text == ADD_CHANNEL_LABEL:
            await message.answer("Пришлите @имя канала или ссылку t.me. Можно несколько через пробел.")
            return
        action = next((action for action, label in MENU_LABELS.items() if label == message.text), None)
        if action is None:
            action = MENU_ACTIONS[message.text]
        if action == "add":
            await message.answer("Пришлите @имя канала или ссылку t.me. Можно несколько через пробел.")
            return
        handlers = {"sources": sources, "pending": pending, "platforms": platforms,
                    "latest": latest, "top": top, "status": status, "check": check, "today": today, "discover": discover,
                    "stats": stats, "analytics": analytics, "insights": insights, "settings": settings}
        await handlers[action](message)

    @router.message()
    async def channel_list(message: Message):
        raw = (message.text or "").strip()
        pending = pending_input.pop(message.from_user.id, None) if message.from_user else None
        if pending:
            field, job_id = pending
            saved = repo.set_job_note(job_id, raw) if field == "note" else repo.set_job_amount(job_id, raw)
            await message.answer("Сохранено" if saved else "Заказ не найден")
            return
        if raw.startswith("@") or raw.startswith("https://t.me/"):
            await add_channels(message, raw)

    @router.callback_query()
    async def callbacks(callback: CallbackQuery):
        parts = (callback.data or "").split(":")
        try:
            if len(parts) == 2 and parts[0] == "menu" and parts[1] in MENU_LABELS:
                action = parts[1]
                await callback.answer()
                handlers = {"sources": sources, "pending": pending, "platforms": platforms, "latest": latest,
                            "top": top, "status": status, "check": check, "today": today,
                            "discover": discover, "stats": stats, "analytics": analytics,
                            "insights": insights, "settings": settings}
                await handlers[action](callback.message)
                return
            if len(parts) == 3 and parts[0] == "toggle":
                family, key = parts[1:]
                if family == "employment" and key in EMPLOYMENT_LABELS:
                    setting = "employment_" + key
                elif family == "search" and key in DISCOVERY_GROUPS:
                    setting = "search_group_" + key
                else:
                    await callback.answer("Неизвестная настройка", show_alert=True)
                    return
                current = repo.setting(setting, "false" if key == "full_time_role" else "true") == "true"
                repo.set_setting(setting, "false" if current else "true")
                await callback.message.edit_reply_markup(reply_markup=settings_keyboard())
                await callback.answer("Настройка сохранена")
                return
            if len(parts) == 2 and parts[0] == "analytics" and parts[1] in {"7", "30", "all"}:
                days = None if parts[1] == "all" else int(parts[1])
                await callback.message.edit_text(analytics_text(days), reply_markup=analytics_keyboard())
                await callback.answer()
                return
            if len(parts) == 3 and parts[0] == "field" and parts[1] in {"note", "amount"}:
                job_id = int(parts[2])
                if repo.job(job_id) is None:
                    await callback.answer("Заказ не найден", show_alert=True)
                    return
                pending_input[callback.from_user.id] = (parts[1], job_id)
                await callback.message.answer("Пришли заметку для заказа #" + str(job_id) if parts[1] == "note"
                                              else "Пришли сумму и валюту для заказа #" + str(job_id))
                await callback.answer()
                return
            if len(parts) == 3 and parts[0] == "settings":
                await callback.answer("Порог отключён: присылаются все подходящие IT-варианты", show_alert=True)
                return
            if len(parts) == 2 and parts[0] == "platforms" and parts[1] in {"on", "off"}:
                await service.set_platforms_enabled(parts[1] == "on")
                await callback.answer("Площадки включены" if parts[1] == "on" else "Площадки на паузе")
                await platforms(callback.message)
                return
            if len(parts) == 3 and parts[0] == "channel":
                channel_id = int(parts[2])
                initial_cursor = 0
                if parts[1] == "approved":
                    channel = repo.get_channel(channel_id)
                    if channel is None or channel.status != "pending":
                        await callback.answer("Канал уже обработан", show_alert=True)
                        return
                    initial_cursor = await service.source.latest_message_id(channel)
                changed = repo.transition_channel(channel_id, parts[1], initial_cursor)
                if changed and (getattr(callback.message, "text", "") or "").startswith("Новые чаты на рассмотрение"):
                    rows = [row for row in callback.message.reply_markup.inline_keyboard
                            if not any(button.callback_data == f"channel:{parts[1]}:{channel_id}" or
                                       button.callback_data == f"channel:{'rejected' if parts[1] == 'approved' else 'approved'}:{channel_id}"
                                       for button in row)]
                    await callback.message.edit_reply_markup(
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
                await callback.answer("Статус обновлён" if changed else "Канал уже обработан", show_alert=not changed)
                return
            if len(parts) == 2 and parts[0] == "examples":
                candidate = repo.get_candidate(int(parts[1]))
                examples = candidate.examples if candidate else []
                await callback.message.answer("\n\n".join(examples)[:4000] or "Примеры не найдены")
                await callback.answer()
                return
            if len(parts) == 3 and parts[0] == "feedback" and parts[1] in {*JOB_STATUS_LABELS, "new", "change"}:
                job_id = int(parts[2])
                job = repo.job(job_id)
                if job is None:
                    await callback.answer("Заказ не найден", show_alert=True)
                    return
                link = repo.job_link(job_id)
                if parts[1] == "change":
                    await callback.message.edit_reply_markup(reply_markup=job_keyboard(job_id, link, job.status, editing=True))
                    await callback.answer()
                    return
                repo.add_feedback(job_id, parts[1])
                job = repo.job(job_id)
                analysis = repo.analysis(job_id)
                await callback.message.edit_text(job_text(job, analysis, link)[:4000],
                                                 reply_markup=job_keyboard(job_id, link, job.status))
                await callback.answer("Решение сохранено")
                return
            if len(parts) == 2 and parts[0] in {"plan", "draft"}:
                job_id = int(parts[1])
                original = repo.job_original(job_id)
                if original is None:
                    await callback.answer("Заказ не найден", show_alert=True)
                    return
                await callback.answer("Готовлю ответ")
                result = await service.ai.ask(parts[0], original)
                text = "\n".join(result["plan"] + ["Вопросы:"] + result.get("questions", [])) if parts[0] == "plan" else result["draft"]
                await callback.message.answer(text[:4000])
                return
        except Exception:
            log.exception("Действие кнопки")
            await callback.answer("Не удалось выполнить действие; попробуйте позже", show_alert=True)
            return
        await callback.answer("Неизвестное действие", show_alert=True)

    return router
