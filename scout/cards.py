"""Native Telegram order cards: one message, persistent choices, no web view."""
from __future__ import annotations

import logging
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from .service import CONTACT_LABELS, JOB_STATUS_LABELS

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def safe(value, limit=300):
    text = str(value or "")
    return escape(text[:limit] + ("…" if len(text) > limit else ""))


def card_keyboard(job_id, link=None, status="new", following=False, view="home"):
    def button(text, action):
        return InlineKeyboardButton(text=text, callback_data=f"card:{action}:{job_id}")

    back = [button("← К заказу", "home")]
    if view == "statuses":
        rows = [[InlineKeyboardButton(text=("✓ " if status == key else "") + label,
                                      callback_data=f"feedback:{key}:{job_id}")]
                for key, label in JOB_STATUS_LABELS.items()]
        rows.extend([[InlineKeyboardButton(text="Снять статус", callback_data=f"feedback:new:{job_id}")], back])
    elif view == "notes":
        rows = [[InlineKeyboardButton(text="✏️ Изменить заметку", callback_data=f"field:note:{job_id}"),
                 InlineKeyboardButton(text="💰 Моя цена", callback_data=f"field:amount:{job_id}")], back]
    elif view == "draft":
        rows = [[InlineKeyboardButton(text="✏️ Изменить отклик", callback_data=f"field:draft:{job_id}")],
                [button("🔄 Новый вариант", "redraft")], back]
    elif view == "input":
        rows = [[button("Отменить ввод", "cancel")]]
    elif view in {"plan", "source"}:
        rows = [back]
    else:
        source = (InlineKeyboardButton(text="↗ Объявление", url=link) if link
                  else button("📄 Объявление", "source"))
        rows = [
            [button("⏹ Не отслеживать" if following else "👀 Отслеживать заказ", "unfollow" if following else "follow")],
            [InlineKeyboardButton(text="✍️ Отклик", callback_data=f"draft:{job_id}"),
             InlineKeyboardButton(text="🔎 Разбор", callback_data=f"plan:{job_id}")],
            [source, InlineKeyboardButton(text="↩️ Вернуть заказ" if status == "irrelevant" else "✕ Не подходит",
                                          callback_data=f"feedback:{'new' if status == 'irrelevant' else 'irrelevant'}:{job_id}")],
            [button("📌 Статус", "statuses"), button("📝 Заметка / цена", "notes")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_text(repo, job_id, view="home", notice="", sample=False):
    job = repo.job(job_id)
    analysis = repo.analysis(job_id)
    data = analysis.result if analysis else {}
    heading = "🧪 Тест карточки · сохранённый заказ" if sample else "📋 Заказ"
    parts = [f"{heading} #{job.id}", f"<b>{safe(data.get('title') or job.title or 'Без названия', 180)}</b>"]
    requirements = data.get("requirements") or []
    if requirements:
        parts.append(safe(" · ".join(requirements[:4]), 350))
    budget = str(data.get("budget") or "не указан")
    currency = str(data.get("currency") or "")
    currency_present = (any(unit in budget.lower() for unit in ("₽", "руб", "rub"))
                        if currency.lower() in {"₽", "руб", "rub"} else currency.lower() in budget.lower())
    if data.get("budget") and currency and not currency_present:
        budget += " " + currency
    parts.append(f"💰 <b>{safe(budget, 80)}</b>   ·   ⏱ {safe(data.get('deadline') or 'срок не указан', 70)}\n"
                 f"⭐ <b>{job.score if job.score is not None else '—'}/100</b> · ИИ-рейтинг")
    parts.append(f"{'👀 Отслеживается' if job.saved else 'Не отслеживается'}\n"
                 f"{JOB_STATUS_LABELS.get(job.status, '🆕 Новый')}\n"
                 f"Отклик: {CONTACT_LABELS.get(job.contact_status, 'Не подтверждено')}")
    if view == "home":
        if job.contact_url and job.contact_status in {"direct_free", "platform_free"}:
            parts.append(f"Контакт: {safe(job.contact_url, 250)}")
        if job.amount:
            parts.append(f"<b>Моя цена:</b> {safe(job.amount, 100)}")
        if job.note:
            parts.append(f"<b>Заметка:</b> {safe(job.note, 300)}")
    elif view == "plan":
        plan = repo.analysis(job_id, "plan_card")
        result = plan.result if plan else {}
        body = "\n".join(f"{i}. {line}" for i, line in enumerate(result.get("plan", []), 1))
        if result.get("questions"):
            body += "\n\nЧто уточнить:\n" + "\n".join("• " + q for q in result["questions"])
        parts.append("<b>Разбор и план</b>\n" + safe(body or "Разбор пока не сохранён.", 2100))
    elif view == "draft":
        draft = repo.analysis(job_id, "draft_saved")
        text = draft.result.get("draft", "") if draft else "Отклик пока не подготовлен."
        parts.append("<b>Черновик отклика</b>\n" + safe(text, 2100))
        if len(text) > 2100:
            parts.append("<i>Показаны первые 2100 символов сохранённого черновика.</i>")
        parts.append("<i>Черновик сохраняется у тебя. Заказчику автоматически не отправляется.</i>")
    elif view == "notes":
        parts.append(f"<b>Моя цена:</b> {safe(job.amount or 'не указана', 100)}\n"
                     f"<b>Заметка:</b> {safe(job.note or 'нет заметки', 1000)}")
    elif view == "source":
        parts.append("<b>Исходное объявление</b>\n" + safe(repo.job_original(job_id) or "Текст недоступен", 2100))
    elif view == "statuses":
        parts.append("<b>Выбери этап работы с заказом ↓</b>")
    if notice:
        parts.append(safe(notice, 500))
    if sample:
        parts.append("<i>Пример из базы, не новый заказ. Кнопки сохраняют твои изменения.</i>")
    return "\n\n".join(parts)


class OrderCards:
    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.pending = {}
        self.generating = set()

    async def show(self, message, job_id, view="home", notice=""):
        job = self.repo.job(job_id)
        sample = (getattr(message, "text", "") or "").startswith("🧪")
        try:
            await message.edit_text(card_text(self.repo, job_id, view, notice, sample), parse_mode="HTML",
                                    link_preview_options=NO_PREVIEW,
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
