from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scout.local_tunnel import TUNNEL_URL, publish, save_public_url


def test_only_quick_tunnel_addresses_are_accepted(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=keep-private\nMINIAPP_ENABLED=true\nMINIAPP_URL=old\n")
    url = "https://some-long-test-name.trycloudflare.com"
    save_public_url(url, env)
    assert env.read_text() == "BOT_TOKEN=keep-private\nMINIAPP_ENABLED=true\nMINIAPP_URL=" + url + "\n"
    assert env.stat().st_mode & 0o777 == 0o600
    for bad in ("http://other.example", url + "/path", url + ".other.example", "https://user:pass@other.example"):
        with pytest.raises(ValueError):
            save_public_url(bad, env)
    assert TUNNEL_URL.search("Visit " + url + " for your tunnel")[0] == url


def test_env_append_and_duplicate_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# hello\nBOT_TOKEN=test\n")
    url = "https://one-two-three.trycloudflare.com"
    save_public_url(url, env)
    assert env.read_text().endswith("MINIAPP_URL=" + url + "\n")
    env.write_text("MINIAPP_URL=first\nMINIAPP_URL=second\n")
    save_public_url(url, env)
    assert env.read_text().splitlines() == ["MINIAPP_URL=" + url] * 2


@pytest.mark.parametrize("telegram_suffix", ["", "/"])
async def test_menu_publication_does_not_poll_or_send_messages(repo, monkeypatch, tmp_path, telegram_suffix):
    url = "https://one-two-three.trycloudflare.com"
    bot = SimpleNamespace(set_chat_menu_button=AsyncMock(),
                          get_chat_menu_button=AsyncMock(return_value=SimpleNamespace(web_app=SimpleNamespace(url=url + telegram_suffix))),
                          session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr("scout.local_tunnel.Bot", lambda token: bot)
    monkeypatch.setattr("scout.local_tunnel.verify_public_url", AsyncMock())
    saved = []
    monkeypatch.setattr("scout.local_tunnel.save_public_url", saved.append)
    monkeypatch.setattr("scout.local_tunnel.make_engine", lambda url: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr("scout.local_tunnel.make_session_factory", lambda engine: repo.factory)
    await publish(url, SimpleNamespace(database_url="sqlite://", bot_token="test", owner_id=42))
    assert repo.setting("miniapp_public_url") == url
    assert saved == [url]
    bot.set_chat_menu_button.assert_awaited_once()
    assert bot.set_chat_menu_button.await_args.kwargs["chat_id"] == 42
    bot.session.close.assert_awaited_once()


async def test_failed_public_check_does_not_change_menu_or_database(repo, monkeypatch):
    monkeypatch.setattr("scout.local_tunnel.verify_public_url", AsyncMock(side_effect=RuntimeError("not ready")))
    save = AsyncMock()
    monkeypatch.setattr("scout.local_tunnel.Bot", save)
    with pytest.raises(RuntimeError, match="not ready"):
        await publish("https://one-two-three.trycloudflare.com", SimpleNamespace())
    save.assert_not_called()
    assert repo.setting("miniapp_public_url") == ""
