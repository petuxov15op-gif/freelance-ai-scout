"""Native Telegram order cards: one message, persistent choices, no web view."""
from __future__ import annotations

import logging
import asyncio
import re
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from .service import CONTACT_LABELS, JOB_STATUS_LABELS
from .card_image import render_order_image

log = logging.getLogger(__name__)


def safe(value, limit=300):
    text = str(value or "")
    encoded = text.encode("utf-16-le")
    clipped = encoded[:limit * 2].decode("utf-16-le", errors="ignore")
    return escape(clipped + ("…" if len(encoded) > limit * 2 else ""))


def card_keyboard(job_id, link=None, status="new", following=False, view="home"):
    def button(text, action, style=None):
        return InlineKeyboardButton(text=text, callback_data=f"card:{action}:{job_id}", style=style)

    back = [button("← К заказу", "home")]
    if view == "statuses":
        rows = [[InlineKeyboardButton(text=("✓ " if status == key else "") + label,
                                      callback_data=f"feedback:{key}:{job_id}")]
                for key, label in JOB_STATUS_LABELS.items()]
        rows.extend([[InlineKeyboardButton(text="Снять статус", callback_data=f"feedback:new:{job_id}")], back])
    elif view == "notes":
        rows = [[InlineKeyboardButton(text="✏️ Изменить заметку", callback_data=f"field:note:{job_id}"),
                 InlineKeyboardButton(text="💰 Моя цена", callback_data=f"field:amount:{job_id}")],
                [button("Полный текст .txt", "file_notes")], back]
    elif view == "draft":
        rows = [[InlineKeyboardButton(text="✏️ Изменить отклик", callback_data=f"field:draft:{job_id}")],
                [button("Новый вариант", "redraft")], [button("Полный текст .txt", "file_draft")], back]
    elif view == "input":
        rows = [[button("Отменить ввод", "cancel")]]
    elif view in {"plan", "source"}:
        rows = [[button("Полный текст .txt", "file_" + view)], back]
    else:
        source = (InlineKeyboardButton(text="↗ Объявление", url=link) if link
                  else button("📄 Объявление", "source"))
        rows = [
            [button("◉ Не отслеживать" if following else "◎ Отслеживать заказ", "unfollow" if following else "follow", "primary")],
            [InlineKeyboardButton(text="Написать отклик", callback_data=f"draft:{job_id}"),
             InlineKeyboardButton(text="Разобрать заказ", callback_data=f"plan:{job_id}")],
            [source, InlineKeyboardButton(text="Вернуть заказ" if status == "irrelevant" else "× Не подходит",
                                          callback_data=f"feedback:{'new' if status == 'irrelevant' else 'irrelevant'}:{job_id}")],
            [button("Статус отклика", "statuses"), button("Заметка / цена", "notes")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def budget_text(data):
    budget = str(data.get("budget") or "не указан")
    currency = str(data.get("currency") or "")
    present = (any(unit in budget.lower() for unit in ("₽", "руб", "rub"))
               if currency.lower() in {"₽", "руб", "rub"} else currency.lower() in budget.lower())
    return budget + (" " + currency if data.get("budget") and currency and not present else "")


def detail_text(repo, job_id, view):
    if view == "plan":
        cached = repo.analysis(job_id, "plan_card")
        data = cached.result if cached else {}
        body = "\n".join(f"{i}. {line}" for i, line in enumerate(data.get("plan", []), 1))
        if data.get("questions"):
            body += "\n\nЧто уточнить:\n" + "\n".join("• " + line for line in data["questions"])
        return body or "Разбор пока не сохранён."
    if view == "draft":
        cached = repo.analysis(job_id, "draft_saved")
        return cached.result["draft"] if cached else "Черновик пока не подготовлен."
    if view == "notes":
        job = repo.job(job_id)
        return f"Моя цена: {job.amount or 'не указана'}\n\nЗаметка: {job.note or 'нет заметки'}"
    return repo.job_original(job_id) or "Текст объявления недоступен."


async def card_media(repo, job_id, view="home", notice="", sample=False):
    job = repo.job(job_id)
    analysis = repo.analysis(job_id)
    data = analysis.result if analysis else {}
    channel = repo.job_channel(job_id)
    title = data.get("title") or job.title or "Без названия"
    section = {"plan": "Разбор и план", "draft": "Черновик отклика", "notes": "Заметка и моя цена",
               "source": "Исходное объявление", "statuses": "Статус отклика", "input": "Редактирование"}.get(view, "Заказ")
    state = re.sub(r"^[^\w]+", "", JOB_STATUS_LABELS.get(job.status, "Новый"))
    payload = {"title": title, "summary": " · ".join((data.get("requirements") or [])[:4]),
               "source": f"ЗАКАЗ #{job_id}" + (f"  ·  @{channel.username}" if channel and channel.username else ""),
               "budget": budget_text(data), "deadline": str(data.get("deadline") or "Не указан"),
               "score": f"{job.score if job.score is not None else '—'}/100", "status": state,
               "following": job.saved, "contact": "Отклик: " + CONTACT_LABELS.get(job.contact_status, "Не подтверждено"),
               "amount": job.amount, "note": job.note, "notice": notice, "sample": sample,
               "view": view, "section": section}
    if view in {"plan", "draft", "notes", "source"}:
        body = detail_text(repo, job_id, view)
        caption = f"<b>{section}</b>\n\n" + safe(body, 850)
        if len(body) > 850:
            caption += "\n\n<i>Показан фрагмент. Полный текст доступен отдельным файлом.</i>"
        if view == "draft":
            caption += "\n\n<i>Заказчику не отправляется.</i>"
    elif view == "input":
        caption = safe(notice)
    elif view == "statuses":
        caption = "Выбери этап работы с заказом ↓"
    else:
        caption = (f"🧪 #{job_id}" if sample else f"Заказ #{job_id}") + f" · {safe(state)}"
        if notice:
            caption += "\n" + safe(notice, 350)
    if sample and view != "home":
        caption = "🧪 Пример из базы · " + caption
    image = await asyncio.to_thread(render_order_image, payload)
    return InputMediaPhoto(media=BufferedInputFile(image, filename=f"scout-order-{job_id}.png"),
                           caption=caption, parse_mode="HTML")


async def send_card(bot, repo, owner_id, job_id, sample=False):
    job = repo.job(job_id)
    media = await card_media(repo, job_id, sample=sample)
    return await bot.send_photo(owner_id, media.media, caption=media.caption, parse_mode="HTML",
                                reply_markup=card_keyboard(job_id, repo.job_link(job_id), job.status, job.saved))



class OrderCards:
    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.pending = {}
        self.generating = set()

    async def show(self, message, job_id, view="home", notice=""):
        job = self.repo.job(job_id)
        sample = (getattr(message, "caption", "") or getattr(message, "text", "") or "").startswith("🧪")
        try:
            media = await card_media(self.repo, job_id, view, notice, sample)
            await message.edit_media(media=media,
                                     reply_markup=card_keyboard(job_id, self.repo.job_link(job_id), job.status, job.saved, view))
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def accept_input(self, message):
        user_id = message.from_user.id if message.from_user else None
        pending = self.pending.get(user_id)
        if not pending:
            return False
        field, job_id, origin = pending
        raw = (message.text or "").strip()
        maximum = {"note": 1000, "amount": 100, "draft": 10000}[field]
        if not raw or len(raw) > maximum:
            await message.answer(f"Пришли текст от 1 до {maximum} символов или нажми «Отменить ввод» в карточке.")
            return True
        if not self.repo.job(job_id):
            self.pending.pop(user_id, None)
            await message.answer("Заказ не найден")
            return True
        if field == "draft":
            self.repo.save_analysis(job_id, "draft_saved", "owner", {"draft": raw})
        elif field == "note":
            self.repo.set_job_note(job_id, "" if raw == "-" else raw)
        else:
            self.repo.set_job_amount(job_id, "" if raw == "-" else raw)
        self.pending.pop(user_id, None)
        try:
            await self.show(origin, job_id, "draft" if field == "draft" else "home", "✓ Изменения сохранены")
        except Exception:
            log.exception("Обновление карточки после ввода")
            await message.answer("Изменения сохранены. Открой заказ заново в разделе «Заказы».")
        return True

    async def handle(self, callback):
        parts = (callback.data or "").split(":")
        if len(parts) not in {2, 3}:
            await callback.answer("Неизвестная кнопка", show_alert=True)
            return
        kind = parts[0]
        action = parts[1] if len(parts) == 3 else kind
        job_id = int(parts[-1])
        job = self.repo.job(job_id)
        if not job or not callback.message:
            await callback.answer("Заказ не найден. Открой карточку заново.", show_alert=True)
            return
        self.pending.pop(callback.from_user.id, None)
        view, notice = "home", ""
        if kind == "card" and action in {"file_plan", "file_draft", "file_source", "file_notes"}:
            await callback.answer()
            content = detail_text(self.repo, job_id, action.removeprefix("file_"))
            await callback.message.answer_document(BufferedInputFile(content.encode("utf-8"), filename=f"order-{job_id}-{action[5:]}.txt"))
            return
        if kind == "card" and action in {"home", "statuses", "notes", "source"}:
            view = action
        elif kind == "card" and action in {"follow", "unfollow"}:
            self.repo.set_job_following(job_id, action == "follow")
            if action == "follow" and job.status == "irrelevant":
                self.repo.add_feedback(job_id, "new")
            notice = "✓ Заказ в разделе «Отслеживаемые»" if action == "follow" else "Отслеживание выключено"
        elif kind == "card" and action == "cancel":
            notice = "Ввод отменён"
        elif kind == "field" and action in {"note", "amount", "draft"}:
            self.pending[callback.from_user.id] = (action, job_id, callback.message)
            view = "input"
            notice = {"note": "Напиши заметку следующим сообщением. Чтобы очистить, пришли один дефис: -",
                      "amount": "Пришли свою цену и валюту следующим сообщением. Чтобы очистить, пришли: -",
                      "draft": "Пришли отредактированный отклик следующим сообщением. Он сохранится без отправки заказчику."}[action]
        elif kind == "feedback" and action in {*JOB_STATUS_LABELS, "new", "change"}:
            if action == "change":
                view = "statuses"
            else:
                if job.status != action:
                    self.repo.add_feedback(job_id, action)
                if action == "irrelevant":
                    self.repo.set_job_following(job_id, False)
                notice = "✓ Статус сохранён"
        elif kind in {"plan", "draft"} or (kind == "card" and action == "redraft"):
            view = "draft" if kind == "draft" or action == "redraft" else "plan"
            if job_id in self.generating:
                await callback.answer("Уже готовлю. Результат появится в этой карточке.")
                return
            cached = self.repo.analysis(job_id, "draft_saved" if view == "draft" else "plan_card")
            if cached and action != "redraft":
                await callback.answer()
                await self.show(callback.message, job_id, view)
                return
            self.generating.add(job_id)
            try:
                await callback.answer("Готовлю отклик" if view == "draft" else "Готовлю разбор")
                await self.show(callback.message, job_id, notice="⏳ Готовлю отклик…" if view == "draft" else "⏳ Разбираю заказ…")
                original = self.repo.job_original(job_id)
                if not original:
                    raise ValueError("Нет исходного текста")
                result = await self.service.ai.ask(view, original)
                self.repo.save_analysis(job_id, "draft_saved" if view == "draft" else "plan_card", "owner", result)
                await self.show(callback.message, job_id, view)
            except Exception:
                log.exception("Подготовка %s для карточки %s", view, job_id)
                await self.show(callback.message, job_id, notice="Не удалось получить ответ ИИ. Попробуй ещё раз; остальные кнопки доступны.")
            finally:
                self.generating.discard(job_id)
            return
        else:
            await callback.answer("Неизвестная кнопка", show_alert=True)
            return
        await callback.answer()
        await self.show(callback.message, job_id, view, notice)
