from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    ai_provider: str
    alternate_api_key: str
    alternate_api_base_url: str
    alternate_model: str
    bot_token: str
    owner_id: int
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_proxy_url: str
    database_url: str
    session_path: str
    daily_cost_limit_usd: float
    ai_concurrency: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        provider = os.getenv("AI_PROVIDER", "openrouter").strip().lower()
        if provider not in {"openrouter", "compatible"}:
            raise ValueError("AI_PROVIDER должен быть openrouter или compatible")
        required = ["BOT_TOKEN", "OWNER_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"]
        required += ["OPENROUTER_API_KEY"] if provider == "openrouter" else ["ALTERNATE_API_KEY", "ALTERNATE_API_BASE_URL", "ALTERNATE_MODEL"]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ValueError("Не заданы переменные: " + ", ".join(missing))
        base_url = os.getenv("ALTERNATE_API_BASE_URL", "").rstrip("/")
        if provider == "compatible" and not base_url.startswith("https://"):
            raise ValueError("ALTERNATE_API_BASE_URL должен начинаться с https://")
        return cls(
            ai_provider=provider,
            alternate_api_key=os.getenv("ALTERNATE_API_KEY", ""),
            alternate_api_base_url=base_url,
            alternate_model=os.getenv("ALTERNATE_MODEL", ""),
            bot_token=os.environ["BOT_TOKEN"],
            owner_id=int(os.environ["OWNER_ID"]),
            telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
            telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
            telegram_phone=os.getenv("TELEGRAM_PHONE", ""),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            openrouter_proxy_url=os.getenv("OPENROUTER_PROXY_URL", ""),
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/scout.db"),
            session_path=str(Path(os.getenv("SESSION_PATH", "~/.local/share/freelance-ai-scout/telegram_user")).expanduser()),
            daily_cost_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "1")),
            ai_concurrency=max(1, int(os.getenv("AI_CONCURRENCY", "2"))),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
