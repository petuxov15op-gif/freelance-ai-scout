import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from scout.models import Job
from scout.service import ScoutService
from scout.web import authenticated_owner, create_web_app, public_link

TOKEN = "123456:unit-test-only"
OWNER = 42


def signed_data(user=OWNER, age=0, token=TOKEN, **extra):
    fields = {"auth_date": str(int((datetime.now(timezone.utc) - timedelta(seconds=age)).timestamp())),
              "user": json.dumps({"id": user, "first_name": "Test"}), **extra}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def web_service(repo):
    source = SimpleNamespace(latest_message_id=AsyncMock(return_value=42))
    ai = SimpleNamespace(ask=AsyncMock(return_value={"draft": "Здравствуйте! Уточните сценарии записи.", "case_id": None}))
    return ScoutService(repo, source, ai, SimpleNamespace(), OWNER)


@pytest.fixture
async def client(web_service):
    async with TestClient(TestServer(create_web_app(web_service, TOKEN, OWNER, "https://scout.example"))) as client:
        yield client


@pytest.fixture
def headers():
    return {"Authorization": "tma " + signed_data()}


@pytest.fixture
def job(repo):
    channel = repo.candidate(100, "testjobs", "Тестовые заказы", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved", 1)
    message = repo.save_message(channel.id, 2, "Нужен Telegram-бот для записи клиентов.", "https://t.me/testjobs/2",
                                datetime.now(timezone.utc), {})
    job, _ = repo.attach_job(message)
    repo.save_analysis(job.id, "deep", "test-model", {"title": "Запись клиентов", "score": 4, "budget": None,
                                                      "requirements": ["Бот"], "score_reason": "Разработка"})
    return job


def test_valid_signature_with_additional_telegram_signature():
    authenticated_owner(signed_data(signature="telegram-ed25519-field"), TOKEN, OWNER)


@pytest.mark.parametrize("raw", ["", "user=bad", "hash=none&auth_date=0", signed_data(age=3601),
                               signed_data(age=-61), signed_data(token="different-bot"),
                               signed_data() + "&auth_date=0", "hash=x&hash=y"])
def test_rejects_invalid_expired_future_and_ambiguous_credentials(raw):
    with pytest.raises(web.HTTPUnauthorized):
        authenticated_owner(raw, TOKEN, OWNER)


def test_rejects_other_telegram_users():
    with pytest.raises(web.HTTPForbidden):
        authenticated_owner(signed_data(user=7), TOKEN, OWNER)


@pytest.mark.parametrize("path", ["/api/state", "/api/jobs", "/api/jobs/1/draft", "/api/channels"])
async def test_every_api_requires_authentication(client, path):
    response = await client.get(path)
    assert response.status == 401
    assert "error" in await response.json()


async def test_does_not_accept_credentials_from_query_string(client):
    response = await client.get("/api/state", params={"initData": signed_data()})
    assert response.status == 401


async def test_other_owner_and_cross_origin_rejected(client, headers):
    assert (await client.get("/api/state", headers={"Authorization": "tma " + signed_data(user=7)})).status == 403
    response = await client.post("/api/monitoring", headers={**headers, "Origin": "https://other.example"}, json={"paused": True})
    assert response.status == 403


async def test_empty_state_contains_no_demo_jobs_or_counts(client, headers):
    response = await client.get("/api/state", headers=headers)
    assert response.status == 200
    state = await response.json()
    assert state["jobs"] == {"items": [], "total": 0, "next_offset": None}
    assert state["channels"] == []
    assert state["statistics"] == {"found": 0, "matched": 0, "applied": 0, "bars": [0] * 12}
    assert response.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in response.headers


async def test_real_state_does_not_drop_low_ratings(client, headers, job):
    state = await (await client.get("/api/state", headers=headers)).json()
    assert state["jobs"]["items"][0]["id"] == job.id
    assert state["jobs"]["items"][0]["score"] == 4
    assert state["jobs"]["items"][0]["budget"] is None
    assert state["statistics"]["found"] == sum(state["statistics"]["bars"]) == 1
    assert state["statistics"]["matched"] == 0  # Contact is not confirmed free.


async def test_feedback_saved_and_duplicate_tap_is_idempotent(client, headers, job, repo):
    for _ in range(2):
        response = await client.post(f"/api/jobs/{job.id}/feedback", headers=headers, json={"status": "applied"})
        assert response.status == 200
    assert repo.job(job.id).status == "applied"
    assert repo.analytics(1)["applied"] == 1
    assert (await client.post(f"/api/jobs/{job.id}/feedback", headers=headers, json={"status": []})).status == 400


async def test_draft_generated_once_then_owner_edits_persist(client, headers, job, repo, web_service):
    url = f"/api/jobs/{job.id}/draft"
    first = await (await client.post(url, headers=headers)).json()
    assert first["draft"].startswith("Здравствуйте!")
    assert (await client.post(url, headers=headers)).status == 200
    web_service.ai.ask.assert_awaited_once_with("draft", repo.job_original(job.id))
    assert (await client.put(url, headers=headers, json={"draft": "Моя правка"})).status == 200
    assert (await (await client.post(url, headers=headers)).json())["draft"] == "Моя правка"
    assert (await client.put(url, headers=headers, json={"draft": ""})).status == 400
    assert (await client.post("/api/jobs/9999/draft", headers=headers)).status == 404


async def test_new_channel_requires_approval_and_preserves_cursor(client, headers, repo, web_service):
    channel = repo.candidate(101, "candidatejobs", "Кандидат", "", None, 50, "Проверить", ["Пример задачи"])
    web_service.add_channel = AsyncMock(return_value=channel)
    response = await client.post("/api/channels", headers=headers, json={"username": "@candidatejobs"})
    assert response.status == 201
    assert (await response.json())["channel"]["status"] == "pending"
    assert repo.list_channels("approved") == []
    assert (await client.post(f"/api/channels/{channel.id}/status", headers=headers, json={"status": "approved"})).status == 200
    assert repo.cursor(channel.id) == 42
    assert repo.get_channel(channel.id).status == "approved"
    assert (await client.post(f"/api/channels/{channel.id}/status", headers=headers, json={"status": "paused"})).status == 200
    assert repo.get_channel(channel.id).status == "paused"
    assert (await client.post(f"/api/channels/{channel.id}/status", headers=headers, json={"status": "rejected"})).status == 409
    assert (await client.post("/api/channels", headers=headers, json={"username": "https://evil.example/name"})).status == 400


async def test_pause_persisted_and_scheduled_monitor_skips_reads(client, headers, web_service):
    web_service.process_pending = AsyncMock()
    assert (await client.post("/api/monitoring", headers=headers, json={"paused": True})).status == 200
    await web_service.monitor()
    assert web_service.last_monitor_started is None
    web_service.process_pending.assert_not_awaited()
    assert web_service.repo.setting("monitoring_paused") == "true"
    assert (await client.post("/api/monitoring", headers=headers, json={"paused": "true"})).status == 400


async def test_pagination_does_not_omit_orders(client, headers, repo):
    from scout.db import transaction
    with transaction(repo.factory) as db:
        db.add_all(Job(canonical_hash=f"test-{i}", title=f"Order {i}", score=1) for i in range(31))
    first = await (await client.get("/api/jobs", headers=headers)).json()
    second = await (await client.get("/api/jobs?offset=30", headers=headers)).json()
    assert len(first["items"]) == 30 and first["next_offset"] == 30
    assert len(second["items"]) == 1 and second["next_offset"] is None
    assert len({j["id"] for j in first["items"] + second["items"]}) == 31
    assert (await client.get("/api/jobs?offset=-1", headers=headers)).status == 400


async def test_static_allowlist_and_bad_requests(client, headers):
    assert (await client.get("/")).status == 200
    assert (await client.get("/live.js")).status == 200
    for path in ("/.env", "/README.md", "/scout.db"):
        assert (await client.get(path)).status == 404
    assert (await client.get("/api/state?period=invalid", headers=headers)).status == 400
    assert (await client.post("/api/monitoring", headers=headers, data="text")).status == 400
    assert (await client.post("/api/monitoring", headers=headers, json=[])).status == 400


@pytest.mark.parametrize("value", ["javascript:alert(1)", "file:///tmp/key", "https://user:pass@host/path", "https://[bad"])
def test_unsafe_links_are_not_exposed(value):
    assert public_link(value) is None
