from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import MenuButtonCommands
from alembic import command
from alembic.config import Config as AlembicConfig
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .ai import OpenRouter
from .bot import bot_commands, create_router
from .config import Config
from .db import make_engine, make_session_factory
from .repository import Repository
from .service import ScoutService
from .telegram import TelegramSource


async def run() -> None:
    config = Config.from_env()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Path(config.session_path).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.database_url)
    alembic_config = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", config.database_url)
    command.upgrade(alembic_config, "head")
    repo = Repository(make_session_factory(engine))
    repo.seed()
    source = TelegramSource(config.telegram_api_id, config.telegram_api_hash, config.session_path, config.telegram_phone)
    bot = Bot(config.bot_token)
    ai = OpenRouter(config.openrouter_api_key, config.openrouter_model, repo, config.daily_cost_limit_usd,
                    config.ai_concurrency, config.openrouter_proxy_url)
    service = ScoutService(repo, source, ai, bot, config.owner_id)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(service))
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Yekaterinburg"))
    scheduler.add_job(service.monitor, "interval", minutes=10, id="monitor", max_instances=1, coalesce=True)
    scheduler.add_job(service.discover, "cron", hour=12, minute=0, id="discover", max_instances=1, coalesce=True)
    initial_monitor = None
    try:
        await source.start()
        try:
            await bot.set_my_commands(bot_commands())
            await bot.set_chat_menu_button(chat_id=config.owner_id, menu_button=MenuButtonCommands())
        except Exception:
            logging.exception("Не удалось обновить меню бота")
        scheduler.start()
        logging.info("Мониторинг каждые 10 минут; поиск новых чатов ежедневно в 12:00 по Екатеринбургу")
        initial_monitor = asyncio.create_task(service.monitor())
        await dispatcher.start_polling(bot, handle_signals=True)
    finally:
        if initial_monitor:
            initial_monitor.cancel()
            with suppress(asyncio.CancelledError):
                await initial_monitor
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await ai.close()
        await source.close()
        await bot.session.close()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
