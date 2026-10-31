import asyncio
from pathlib import Path

from .config import Config
from .telegram import TelegramSource


async def main():
    config = Config.from_env()
    Path(config.session_path).parent.mkdir(parents=True, exist_ok=True)
    source = TelegramSource(config.telegram_api_id, config.telegram_api_hash, config.session_path, config.telegram_phone)
    try:
        if config.telegram_phone:
            await source.client.start(phone=config.telegram_phone)
        else:
            await source.client.start()
        print("Telegram-сессия создана:", config.session_path)
    finally:
        await source.close()


if __name__ == "__main__":
    asyncio.run(main())
