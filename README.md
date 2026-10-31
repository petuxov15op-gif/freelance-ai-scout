# Freelance AI Scout

![Freelance AI Scout cover](portfolio/ai-scout-cover.jpg)

A personal Telegram bot that monitors owner-approved channels for relevant freelance software projects and AI-assisted development roles. It filters posts, explains why an opportunity fits, and sends candidates to the owner. It can draft a reply, but never contacts clients on its own.

## What it does

- Adds a public Telegram channel only after the owner confirms it in the bot's menu.
- Checks approved channels every 10 minutes and stores progress in SQLite so posts can be processed across restarts.
- Filters out unrelated remote jobs and posts without a concrete software task. One-off software projects do not need to mention AI. Ongoing roles must explicitly involve AI-assisted development.
- Uses OpenRouter for structured analysis of candidate posts and delivers every qualifying opportunity without a score threshold.
- Groups exact and similar reposts, supports feedback, and prepares reply drafts for the owner's review.

The bot does not discover channels automatically, run global Telegram searches, join channels on its own, or send applications to clients.

## Stack

Python 3.11+, Telegram Bot API and a Telegram user session, OpenRouter, SQLite, SQLAlchemy, Alembic, Docker Compose, and pytest.

## Local setup

Create a Python environment, install dependencies, and copy the configuration template:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set the required values in `.env`:

| Variable | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `OWNER_ID` | Numeric Telegram ID allowed to control the bot |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Credentials for reading channels with a Telegram user account |
| `OPENROUTER_API_KEY` | Key for candidate analysis |
| `OPENROUTER_MODEL` | Model/provider with JSON Schema structured output support |

The optional `TELEGRAM_PHONE` is used for the first interactive login. Keep `.env` and the resulting Telegram session private. Then authorize the user account and start the bot:

```bash
python -m scout.auth
python -m scout.main
```

Only one instance should poll the Bot API with a given token at a time.

## Docker Compose

For a server deployment, provide a private `.env` and create the Telegram user session interactively before starting the service:

```bash
docker compose build
docker compose run --rm scout python -m scout.auth
docker compose up -d
docker compose logs -f scout
```

The named `scout_data` volume holds the SQLite database and Telegram session. Preserve it during updates.

## Using the bot

Send `/start` to show the menu. Use **Add channel**, send a public `@username` or `t.me` link, and confirm **Add** on the channel card. The menu also exposes sources, pending items, recent matches, statistics, and settings. Commands include `/status`, `/sources`, `/pending`, `/latest`, `/top`, `/stats`, `/settings`, and `/add`.

## Tests

```bash
pytest -q
```

The app applies Alembic migrations at startup. A manual `alembic upgrade head` uses the database path in `alembic.ini`; the app itself reads `DATABASE_URL` from `.env`.

## Notes

- Some public channels restrict history access. The bot logs those errors and retains its stored cursor.
- OpenRouter spending estimates depend on the cost data reported by the provider.
- A rare duplicate notification is possible if the process stops between a Telegram send and the database update.
- Live Telegram and OpenRouter access requires your own credentials and cannot be verified by the test suite.

This repository contains application code and configuration examples only. Do not commit `.env`, Telegram sessions, databases, SSH keys, or VPN configuration.
