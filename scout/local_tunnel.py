"""Keep a local Quick Tunnel's changing address in sync with the owner menu.

Run separately from scout.main; this process never polls Telegram updates.
The cloudflared binary must already be installed from its official release.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import tempfile
from pathlib import Path

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo

from .config import Config
from .db import make_engine, make_session_factory
from .repository import Repository

log = logging.getLogger(__name__)
TUNNEL_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com\b")


def save_public_url(url: str, path: Path = Path(".env")) -> None:
    if not TUNNEL_URL.fullmatch(url):
        raise ValueError("Not a Quick Tunnel HTTPS address")
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    for index, line in enumerate(lines):
        if line.partition("=")[0].strip() == "MINIAPP_URL":
            lines[index] = "MINIAPP_URL=" + url
            found = True
    if not found:
        lines.append("MINIAPP_URL=" + url)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write("\n".join(lines) + "\n")
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def publish(url: str, config: Config) -> None:
    # The shared SQLite setting updates API origin checks without restarting polling.
    engine = make_engine(config.database_url)
    try:
        repo = Repository(make_session_factory(engine))
        repo.set_setting("miniapp_public_url", url)
        save_public_url(url)
        bot = Bot(config.bot_token)
        try:
            for attempt in range(6):
                try:
                    await bot.set_chat_menu_button(
                        chat_id=config.owner_id,
                        menu_button=MenuButtonWebApp(text="Открыть Scout", web_app=WebAppInfo(url=url)),
                    )
                    menu = await bot.get_chat_menu_button(chat_id=config.owner_id)
                    if getattr(getattr(menu, "web_app", None), "url", None) != url:
                        raise RuntimeError("Menu readback did not match")
                    log.info("Кнопка Mini App обновлена: %s", url)
                    return
                except Exception as exc:
                    # Exception strings can contain Bot API URLs; log only the class.
                    log.warning("Обновление кнопки: %s", type(exc).__name__)
                    if attempt == 5:
                        raise RuntimeError("Could not update owner menu") from None
                    await asyncio.sleep(5)
        finally:
            await bot.session.close()
    finally:
        engine.dispose()


async def run() -> None:
    config = Config.from_env()
    if not config.miniapp_enabled or config.miniapp_host != "127.0.0.1":
        raise ValueError("Enable Mini App on 127.0.0.1 before starting a tunnel")
    binary = Path(sys.executable).parent / "cloudflared"
    if not binary.is_file():
        raise FileNotFoundError("Official cloudflared binary missing beside Python")
    # Do not give an external binary the Telegram or AI credentials loaded by Config.
    child_env = {key: value for key, value in os.environ.items()
                 if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
    process = await asyncio.create_subprocess_exec(
        str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2",
        "--url", f"http://127.0.0.1:{config.miniapp_port}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=child_env,
    )
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    announced = None
    try:
        async for line in process.stdout:
            text = line.decode("utf-8", errors="replace")
            match = TUNNEL_URL.search(text)
            if match and match[0] != announced:
                announced = match[0]
                await publish(announced, config)
            elif "Registered tunnel connection" in text:
                log.info("HTTPS-туннель подключён")
            elif " ERR " in text:
                log.warning("Туннель сообщает об ошибке соединения")
        raise RuntimeError("cloudflared exited")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # aiohttp/aiogram debug logging must not disclose token-bearing request URLs.
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        log.error("Туннель остановлен: %s", type(exc).__name__)
        sys.exit(1)
