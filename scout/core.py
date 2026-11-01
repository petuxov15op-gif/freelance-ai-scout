from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value: str) -> str:
    value = re.sub(r"https?://\S+", " ", value.lower())
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def mentions_kwork(value: str) -> bool:
    return bool(re.search(r"(?<!\w)(?:kwork|кворк)\w*", value, re.IGNORECASE))


def has_direct_contact(value: str) -> bool:
    contact = r"(?:@[A-Za-z][A-Za-z0-9_]{4,31}\b|(?:https?://)?t\.me/[A-Za-z][A-Za-z0-9_]{4,31}\b)"
    invitation = r"(?:пишите|писать|напишите|обращайтесь|связь|контакт|заказчик|в\s+лс|в\s+личк\w*|dm\b|contact\b)"
    return bool(re.search(rf"(?:{invitation}).{{0,80}}{contact}|{contact}.{{0,80}}(?:{invitation})", value, re.IGNORECASE))


def text_hash(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode()).hexdigest()


def normalize_link(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")))
    return urlunsplit(("https", parts.hostname.lower(), parts.path.rstrip("/"), query, ""))


def likely_job(text: str) -> bool:
    normalized = normalize_text(text)
    if not potential_job(text):
        return False
    terms = ("нужен", "нужна", "нужно", "ищем", "задача", "требуется", "заказ", "бюджет", "оплата",
             "looking for", "need a", "hiring", "project", "budget", "necesitamos", "buscamos",
             "recherche", "cherche un", "suche einen", "suche eine", "precisamos", "procuro")
    return any(term in normalized for term in terms)


def potential_job(text: str) -> bool:
    normalized = normalize_text(text)
    if len(normalized) < 20:
        return False
    negative = ("предлагаю свои услуги", "наши услуги", "подпишитесь", "курс по", "ищу работу",
                "как находить заказчиков", "использовать бота", "looking for work", "our services")
    if any(term in normalized for term in negative):
        return False
    return True


def explicit_it_task(text: str) -> bool:
    normalized = normalize_text(text)
    technical_terms = (
        "разработ", "программ", "сайт", "лендинг", "веб", "бот", "чат бот",
        "автоматизац", "интеграц", "парсинг", "скрипт", "базу данных", "базы данных",
        "приложен", "интерфейс", "ui ux", "ux ui", "машинн обучен", "аналитик данных",
        "верстк", "тестиров", "интернет магазин", "crm", "сервер", "devops", "llm",
        "вайбкод", "вайткод", "код", "coding", "codex",
        "data science", "machine learning", "frontend", "backend", "front end", "back end",
        "developer", "software", "website", "web app", "mobile app", "automation",
        "integration", "python", "javascript", "typescript", "react", "django", "fastapi",
        "wordpress", "tilda", "figma", "api", "sql", "1с", "1c",
    )
    return any(re.search(r"\b" + re.escape(term), normalized) for term in technical_terms)


def permanent_role(text: str) -> bool:
    normalized = normalize_text(text)
    terms = ("тип работы постоянная", "полная занятость", "постоянная занятость", "постоянную работу",
             "на постоянку", "постоянн", "полный день", "в штат", "трудоустройство",
             "оформление по тк", "зарплата", "оклад", "график 5 2",
             "вакансия", "вакансии", "full time", "fulltime", "salary")
    return any(re.search(r"\b" + re.escape(term), normalized) for term in terms)


def ongoing_role(text: str) -> bool:
    normalized = normalize_text(text)
    if permanent_role(text):
        return True
    recurring = ("частичная занятость", "неполная занятость", "неполный рабочий день",
                 "неполный день", "регулярная работа", "регулярная подработка",
                 "долгосрочное сотрудничество", "на регулярной основе", "part time", "parttime")
    if any(term in normalized for term in recurring):
        return True
    one_off = ("разовая подработка", "разовый заказ", "разовая задача", "на один день",
               "один раз", "единоразов", "one off", "one time")
    return "подработк" in normalized and not any(term in normalized for term in one_off)


def explicit_ai_coding(text: str) -> bool:
    normalized = normalize_text(text)
    terms = ("вайбкод", "вайб код", "вайткод", "vibe cod", "vibecod", "codex", "cursor", "claude code",
             "разработка с ии", "программирование с ии", "код с ии", "кодинг с ии",
             "разработка с ai", "ai assisted coding", "ai coding")
    return any(re.search(r"\b" + re.escape(term), normalized) for term in terms)


def similar(a: str, b: str) -> bool:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return False
    return SequenceMatcher(None, na[:2500], nb[:2500]).ratio() >= 0.88


WEIGHTS = {
    "feasibility": .35,
    "financial": .25,
    "risk": .20,
    "clarity": .10,
    "timeline": .10,
}


def calculate_score(factors: dict) -> int:
    if set(factors) != set(WEIGHTS):
        raise ValueError("Неверный набор факторов рейтинга")
    if any(type(factors[k]) not in (int, float) or not 0 <= factors[k] <= 100 for k in WEIGHTS):
        raise ValueError("Факторы должны быть числами от 0 до 100")
    return round(sum(factors[k] * weight for k, weight in WEIGHTS.items()))
