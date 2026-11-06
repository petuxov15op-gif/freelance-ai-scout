"""Public work the owner has verified for use in reply drafts."""

import re

CASES = {
    "freelance_ai_scout": {
        "description": (
            "Telegram-бот для поиска IT-заказов: читает одобренные каналы, "
            "отбирает подходящие объявления и присылает их владельцу. "
            "Владелец может запросить черновик отклика."
        ),
        "url": "https://github.com/petuxov15op-gif/freelance-ai-scout",
        "reply_line": "Похожий бот уже делал:",
    },
}


def relevant_cases(announcement: str) -> dict:
    """Offer the known bot project only for matching monitoring tasks."""
    text = announcement.lower()
    if (re.search(r"\b(?:бот\w*|telegram)\b", text)
            and re.search(r"\b(?:канал\w*|объявлен\w*|заказ\w*)\b", text)
            and re.search(r"\b(?:монитор\w*|отслеж\w*|поиск\w*|фильтр\w*|собира\w*)\b", text)):
        return CASES
    return {}
