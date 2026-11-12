import pytest

from scout.config import Config


@pytest.fixture(autouse=True)
def config_environment(monkeypatch):
    monkeypatch.setattr("scout.config.load_dotenv", lambda: None)
    for key, value in {"AI_PROVIDER": "openrouter", "BOT_TOKEN": "test", "OWNER_ID": "42",
                       "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "test", "OPENROUTER_API_KEY": "test"}.items():
        monkeypatch.setenv(key, value)
    for key in ("MINIAPP_ENABLED", "MINIAPP_HOST", "MINIAPP_PORT", "MINIAPP_URL"):
        monkeypatch.delenv(key, raising=False)


def test_miniapp_disabled_and_local_by_default():
    config = Config.from_env()
    assert not config.miniapp_enabled
    assert config.miniapp_host == "127.0.0.1"
    assert config.miniapp_port == 8080
    assert config.miniapp_url == ""


def test_local_server_can_be_enabled_before_public_url_exists(monkeypatch):
    monkeypatch.setenv("MINIAPP_ENABLED", "true")
    assert Config.from_env().miniapp_enabled


@pytest.mark.parametrize("url", ["http://scout.example", "https://scout.example/app", "https://name:password@scout.example",
                                "https://scout.example?token=value", "https://scout.example#orders"])
def test_public_url_requires_an_https_origin(monkeypatch, url):
    monkeypatch.setenv("MINIAPP_URL", url)
    with pytest.raises(ValueError, match="MINIAPP_URL"):
        Config.from_env()


def test_valid_public_origin_is_normalized(monkeypatch):
    monkeypatch.setenv("MINIAPP_URL", "https://scout.example/")
    assert Config.from_env().miniapp_url == "https://scout.example"


def test_invalid_port_rejected(monkeypatch):
    monkeypatch.setenv("MINIAPP_PORT", "0")
    with pytest.raises(ValueError, match="MINIAPP_PORT"):
        Config.from_env()
