from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_text(value: str) -> str:
    value = re.sub(r"https?://\S+", " ", value.lower())
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def mentions_platform(value: str) -> bool:
    return bool(re.search(
        r"(?<![A-Za-zА-Яа-я0-9])(?:kwork|кворк\w*|fl\.ru|freelancehunt(?:\.com)?|"
        r"freelancer\.com|freelance\.ru|youdo\.com|weblancer(?:\.net)?|"
        r"workzilla(?:\.com)?|intwork\.top|offersad\.date|jobss\.ru|"
        r"vibejobs\.ru|leadradar)(?![A-Za-zА-Яа-я0-9])",
        value, re.IGNORECASE))


def has_direct_contact_link(value: str, links: list[dict] | None = None,
                            source_username: str | None = None) -> bool:
    if has_direct_contact(value, source_username):
        return True
    for link in links or []:
        label = link.get("label", "")
        url = link.get("url", "")
        match = re.match(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?:[/?#]|$)", url, re.IGNORECASE)
        if (match and re.search(r"(?:напишите|пишите|связаться|связь|контакт|личк|написать|dm\b)", label, re.IGNORECASE)
                and not match[1].lower().endswith("bot")
                and match[1].lower() != (source_username or "").lower()):
            return True
    return False


def has_direct_contact(value: str, source_username: str | None = None) -> bool:
    contact = r"(?:@|(?:https?://)?t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})\b"
    invitation = r"(?:пишите|писать|напишите|обращайтесь|связь|контакт|отклик|в\s+лс|в\s+личк\w*|dm\b|contact\b)"
    for match in re.finditer(contact, value, re.IGNORECASE):
        username = match[1].lower()
        if username.endswith("bot") or username == (source_username or "").lower():
            continue
        nearby = value[max(0, match.start() - 80):match.end() + 80]
        if re.search(invitation, nearby, re.IGNORECASE):
            return True
    return False


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


EMPLOYMENT_TYPES = ("one_time_project", "recurring_project", "part_time_role", "full_time_role")


def employment_type(text: str) -> str:
    value = normalize_text(text)
    if any(term in value for term in ("полная занятость", "полный рабочий день", "полный день", "full time", "fulltime", "график 5 2", "в штат")):
        return "full_time_role"
    if any(term in value for term in ("частичная занятость", "неполная занятость", "неполный день", "part time", "parttime", "часа в день", "часов в неделю", "подработк")) and not any(term in value for term in ("разовая подработка", "разовый заказ")):
        return "part_time_role"
    if any(term in value for term in ("регулярн", "долгосрочн", "на постоянной основе", "постоянные задачи", "retainer")):
        return "recurring_project"
    if permanent_role(text):
        return "full_time_role"
    return "one_time_project"


def contact_options(text: str, links: list[dict] | None = None, source_username: str | None = None,
                    author_username: str | None = None) -> dict:
    """Classify only contact paths evidenced by the announcement or its entities."""
    options = []
    source = (source_username or "").lower()
    invitation = r"(?:пишите|напишите|написать|связаться|контакт|отклик|личк|dm\b|apply|contact|email|почт)"
    evidence = text + " " + " ".join(link.get("label", "") for link in links or [])
    paid = bool(re.search(r"(?<!\w)(?:платн\w*\s+отклик|оплат\w*\s+(?:за\s+)?отклик|отклик\w*\s+за\s+\d|paid\s+application)", evidence, re.I))
    free = bool(re.search(r"(?:бесплатн\w*\s+отклик|отклик\w*\s+бесплатн|free\s+to\s+apply)", evidence, re.I))
    for match in re.finditer(r"(?<![\w])(?:@|https?://t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})\b", text, re.I):
        name = match[1]
        nearby = text[max(0, match.start() - 80):match.end() + 80]
        if name.lower() != source and not name.lower().endswith("bot") and re.search(invitation, nearby, re.I):
            options.append({"status": "direct_free", "method": "Telegram", "url": "https://t.me/" + name})
    for match in re.finditer(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text):
        options.append({"status": "direct_free", "method": "Email", "url": "mailto:" + match[0]})
    if (author_username and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", author_username)
            and re.search(r"(?:пишите|напишите|обращайтесь)\s+автору|(?:dm|message)\s+the\s+author", text, re.I)):
        options.append({"status": "direct_free", "method": "Telegram", "url": "https://t.me/" + author_username})
    plain_links = [{"label": text[max(0, match.start() - 40):match.end() + 40], "url": match[0].rstrip(".,;)")}
                   for match in re.finditer(r"https?://[^\s<>]+", text)]
    for link in [*(links or []), *plain_links]:
        label, url = link.get("label", ""), link.get("url", "")
        parsed = urlsplit(url)
        if parsed.scheme == "mailto" and re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", parsed.path):
            options.append({"status": "direct_free", "method": "Email", "url": url})
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        match = re.fullmatch(r"/([A-Za-z][A-Za-z0-9_]{4,31})/?", parsed.path) if parsed.hostname.lower() in {"t.me", "www.t.me"} else None
        if match and match[1].lower() != source and not match[1].lower().endswith("bot") and re.search(invitation, label, re.I):
            options.append({"status": "direct_free", "method": "Telegram", "url": url})
        elif re.search(r"(?:отклик|подать заявку|заполнить форму|apply|submit application)", label, re.I):
            options.append({"status": "platform_free" if free and not paid else "unknown", "method": "Форма", "url": url})
    if paid:
        return {"status": "paid", "method": options[0]["method"] if options else "Другое", "url": options[0]["url"] if options else None}
    for status in ("direct_free", "platform_free", "unknown"):
        match = next((option for option in options if option["status"] == status), None)
        if match:
            return match
    return {"status": "none", "method": "Другое", "url": None}


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
