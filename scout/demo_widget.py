"""A single interactive notification sample; never tracks real customers."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

STATE_KEY = "demo_order_widget_state"
STATES = {"new": "Ещё не выбран", "following": "✅ Отслеживается", "ignored": "❌ Не подходит"}


def widget_text(state: str = "new", details: bool = False) -> str:
    text = (
        "🧪 ПРОБНОЕ ОБЪЯВЛЕНИЕ · DEMO-001\n\n"
        "Telegram-бот для записи клиентов\n"
        "Бюджет: 35 000 ₽ · срок: 7 дней\n\n"
        "Запись на услуги, напоминания и расписание администратора.\n\n"
        f"Твой выбор: {STATES.get(state, STATES['new'])}\n"
    )
    if details:
        text += ("\nЧто нужно уточнить:\n"
                 "• Какие услуги и сотрудники будут в расписании?\n"
                 "• Нужна ли интеграция с CRM?\n"
                 "• Кто предоставляет тексты и материалы?\n")
    return text + (
        "\nНажми «Отслеживать» — выбор сохранится, а кнопка изменится.\n"
        "Вымышленный пример для проверки кнопок. Реальный поиск и заказы не меняются."
    )


def widget_keyboard(state: str = "new", details: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏹ Остановить отслеживание" if state == "following" else "👀 Отслеживать",
            callback_data="demo:stop" if state == "following" else "demo:follow")],
        [InlineKeyboardButton(text="↩️ Вернуть объявление" if state == "ignored" else "❌ Не подходит",
                              callback_data="demo:reset" if state == "ignored" else "demo:ignore"),
         InlineKeyboardButton(text="← Назад" if details else "ℹ️ Подробнее",
                              callback_data="demo:back" if details else "demo:details")],
        [InlineKeyboardButton(text="🔎 Проверить сохранённый выбор", callback_data="demo:check")],
    ])


async def handle_widget(callback, repo) -> None:
    action = (callback.data or "").removeprefix("demo:")
    changes = {"follow": "following", "stop": "new", "ignore": "ignored", "reset": "new"}
    if action not in {*changes, "details", "back", "check"}:
        await callback.answer("Неизвестная кнопка примера", show_alert=True)
        return
    if action in changes:
        repo.set_setting(STATE_KEY, changes[action])
    state = repo.setting(STATE_KEY, "new")
    if action == "check":
        await callback.answer("Сохранено в базе: " + STATES.get(state, STATES["new"]), show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Открой исходное сообщение с примером", show_alert=True)
        return
    try:
        await callback.message.edit_text(widget_text(state, action == "details"),
                                         reply_markup=widget_keyboard(state, action == "details"))
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer("Выбор сохранён: " + STATES.get(state, STATES["new"]))
