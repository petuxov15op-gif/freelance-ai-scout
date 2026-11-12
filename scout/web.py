"""Owner-only Mini App API, running beside the existing polling dispatcher."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data
from sqlalchemy import func, select

from .models import Job, Message
from .service import JOB_STATUS_LABELS, CONTACT_LABELS

log = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parent.parent / "miniapp"
PERIODS = {"day": (1, 12), "week": (7, 7), "month": (30, 10)}


def authenticated_owner(raw: str, token: str, owner_id: int, now: datetime | None = None) -> None:
    """Check Telegram's HMAC, owner, ambiguity, and a one-hour credential age."""
    try:
        pairs = parse_qsl(raw, strict_parsing=True, keep_blank_values=True, max_num_fields=30)
        if not raw or len(raw) > 16384 or len(pairs) != len(dict(pairs)):
            raise ValueError("Ambiguous initData")
        data = safe_parse_webapp_init_data(token, raw)
        current = now or datetime.now(timezone.utc)
        age = (current - data.auth_date).total_seconds()
        if age < -30 or age > 3600:
            raise ValueError("Expired initData")
    except (ValueError, TypeError, OverflowError) as exc:
        raise web.HTTPUnauthorized(text="Открой приложение заново из меню бота") from exc
    if data.user is None or data.user.id != owner_id:
        raise web.HTTPForbidden(text="Доступ разрешён только владельцу бота")


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def public_link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


def create_web_app(service, token: str, owner_id: int, public_url: str = "") -> web.Application:
    repo = service.repo
    mutation_lock = asyncio.Lock()

    @web.middleware
    async def security(request, handler):
        try:
            if request.path.startswith("/api/"):
                header = request.headers.get("Authorization", "")
                if not header.startswith("tma "):
                    raise web.HTTPUnauthorized(text="Открой приложение из меню бота")
                authenticated_owner(header[4:], token, owner_id)
                expected_origin = repo.setting("miniapp_public_url", public_url)
                if request.headers.get("Origin") and expected_origin and request.headers["Origin"] != expected_origin:
                    raise web.HTTPForbidden(text="Недопустимый источник запроса")
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text}, status=exc.status)
        except Exception:
            log.exception("Ошибка Mini App: %s %s", request.method, request.path)
            response = web.json_response({"error": "Не удалось выполнить действие. Попробуй позже."}, status=500)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors https://web.telegram.org https://*.telegram.org; form-action 'self'"
            ),
        })
        return response

    app = web.Application(middlewares=[security], client_max_size=32768)

    async def body(request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="Нужен JSON-запрос")
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise web.HTTPBadRequest(text="Некорректный JSON") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Нужен JSON-объект")
        return data

    def identity(request):
        try:
            value = int(request.match_info["id"])
            if value < 1:
                raise ValueError
            return value
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Некорректный идентификатор") from exc

    def get_job(request):
        job = repo.job(identity(request))
        if job is None:
            raise web.HTTPNotFound(text="Заказ не найден")
        return job

    def job_data(job):
        deep = repo.analysis(job.id)
        analysis = deep.result if deep else {}
        message = repo.job_message(job.id)
        channel = repo.job_channel(job.id)
        return {
            "id": job.id, "title": job.title or "Без названия", "score": job.score,
            "status": job.status, "status_label": JOB_STATUS_LABELS.get(job.status, "Новый"),
            "budget": analysis.get("budget"), "currency": analysis.get("currency"),
            "description": (message.text[:2000] if message else "Описание недоступно"),
            "source": channel.title if channel else "Источник недоступен",
            "published_at": iso(message.published_at) if message else None,
            "link": public_link(repo.job_link(job.id)),
            "contact_label": CONTACT_LABELS.get(job.contact_status, "Не подтверждено"),
            "score_reason": analysis.get("score_reason", "Пояснение не сохранено"),
            "requirements": analysis.get("requirements") or [],
            "risks": analysis.get("risks") or [], "questions": analysis.get("questions") or [],
        }

    def jobs_page(offset=0):
        with repo.factory() as db:
            query = select(Job).where(Job.score.is_not(None)).order_by(Job.id.desc())
            rows = list(db.scalars(query.offset(offset).limit(30)))
            total = db.scalar(select(func.count()).select_from(Job).where(Job.score.is_not(None))) or 0
        return {"items": [job_data(j) for j in rows], "total": total,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def channel_data(channel):
        candidate = repo.get_candidate(channel.id) if channel.status == "pending" else None
        return {"id": channel.id, "name": f"@{channel.username}" if channel.username else channel.title,
                "title": channel.title, "status": channel.status,
                "platform_paused": service.source_paused(channel),
                "reason": candidate.reason if candidate else "",
                "examples": candidate.examples if candidate else []}

    async def state(request):
        period = request.query.get("period", "day")
        if period not in PERIODS:
            raise web.HTTPBadRequest(text="Неизвестный период")
        days, count = PERIODS[period]
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        bars = [0] * count
        with repo.factory() as db:
            for timestamp in db.scalars(select(Message.discovered_at).where(
                    Message.discovered_at >= since, Message.discovered_at <= now)):
                delta = (timestamp.replace(tzinfo=timezone.utc) - since).total_seconds()
                bars[min(count - 1, int(delta / (days * 86400) * count))] += 1
        analytics = repo.analytics(days)
        channels = repo.monitored_channels() + repo.list_channels("pending")
        return web.json_response({
            "updated_at": now.isoformat(), "period": period,
            "monitoring": {"paused": repo.setting("monitoring_paused", "false") == "true",
                           "running": service.monitor_lock.locked(),
                           "last_started": iso(service.last_monitor_started),
                           "last_finished": iso(service.last_monitor_finished),
                           "errors": len(service.last_monitor_errors)},
            "statistics": {"found": sum(bars), "matched": analytics["unique"],
                           "applied": analytics["applied"], "bars": bars},
            "channels": [channel_data(c) for c in channels], "jobs": jobs_page(),
        })

    async def jobs(request):
        try:
            offset = int(request.query.get("offset", "0"))
            if offset < 0 or offset > 1_000_000:
                raise ValueError
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Некорректная страница") from exc
        return web.json_response(jobs_page(offset))

    async def monitoring(request):
        data = await body(request)
        if type(data.get("paused")) is not bool:
            raise web.HTTPBadRequest(text="Укажи paused: true или false")
        repo.set_setting("monitoring_paused", str(data["paused"]).lower())
        return web.json_response({"ok": True})

    async def feedback(request):
        job = get_job(request)
        data = await body(request)
        action = data.get("status")
        if not isinstance(action, str) or action not in {*JOB_STATUS_LABELS, "new"}:
            raise web.HTTPBadRequest(text="Неизвестный статус")
        if job.status != action:
            repo.add_feedback(job.id, action)
        return web.json_response({"ok": True})

    async def draft(request):
        job = get_job(request)
        # Serialize generation and edits; a repeated tap reuses the saved draft.
        if mutation_lock.locked():
            raise web.HTTPConflict(text="Другое действие ещё выполняется")
        async with mutation_lock:
            saved = repo.analysis(job.id, "draft_saved")
            if request.method == "PUT":
                data = await body(request)
                text = data.get("draft")
                if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                    raise web.HTTPBadRequest(text="Текст отклика: от 1 до 10 000 символов")
            elif saved:
                text = saved.result["draft"]
            else:
                original = repo.job_original(job.id)
                if not original:
                    raise web.HTTPNotFound(text="Текст объявления недоступен")
                result = await service.ai.ask("draft", original)
                text = result["draft"]
            repo.save_analysis(job.id, "draft_saved", "owner", {"draft": text})
        return web.json_response({"draft": text})

    async def add_channel(request):
        data = await body(request)
        username = data.get("username")
        if not isinstance(username, str) or not 1 <= len(username) <= 100:
            raise web.HTTPBadRequest(text="Укажи публичный @username или ссылку t.me")
        from .telegram import parse_public_channel
        try:
            parse_public_channel(username)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if mutation_lock.locked():
            raise web.HTTPConflict(text="Другое действие ещё выполняется")
        async with mutation_lock:
            channel = await service.add_channel(username)
        if channel is None:
            raise web.HTTPConflict(text="Этот канал уже известен боту")
        return web.json_response({"channel": channel_data(channel)}, status=201)

    async def channel_status(request):
        channel_id = identity(request)
        data = await body(request)
        status = data.get("status")
        if not isinstance(status, str) or status not in {"approved", "paused", "rejected"}:
            raise web.HTTPBadRequest(text="Неизвестный статус канала")
        if mutation_lock.locked():
            raise web.HTTPConflict(text="Другое действие ещё выполняется")
        async with mutation_lock:
            channel = repo.get_channel(channel_id)
            if channel is None:
                raise web.HTTPNotFound(text="Канал не найден")
            if channel.status == "pending" and status in {"approved", "rejected"}:
                # Match the bot's approval semantics: start at the newest post.
                async with service.monitor_lock:
                    cursor = await service.source.latest_message_id(channel) if status == "approved" else 0
                    changed = repo.transition_channel(channel.id, status, cursor)
            elif channel.status in {"approved", "paused"} and status in {"approved", "paused"}:
                changed = channel.status == status or await service.set_channel_monitoring(channel.id, status == "approved")
            else:
                changed = False
        if not changed:
            raise web.HTTPConflict(text="Статус изменился. Обнови список каналов.")
        return web.json_response({"ok": True})

    async def asset(request):
        name = request.match_info.get("name", "index.html")
        if name not in {"index.html", "styles.css", "app.js", "live.js"} or not (ASSETS / name).is_file():
            raise web.HTTPNotFound(text="Файл не найден")
        return web.FileResponse(ASSETS / name)

    app.router.add_get("/api/state", state)
    app.router.add_get("/api/jobs", jobs)
    app.router.add_post("/api/monitoring", monitoring)
    app.router.add_post("/api/jobs/{id}/feedback", feedback)
    app.router.add_post("/api/jobs/{id}/draft", draft)
    app.router.add_put("/api/jobs/{id}/draft", draft)
    app.router.add_post("/api/channels", add_channel)
    app.router.add_post("/api/channels/{id}/status", channel_status)
    app.router.add_get("/", asset)
    app.router.add_get("/{name}", asset)
    return app


async def start_web_server(service, config) -> web.AppRunner:
    runner = web.AppRunner(create_web_app(service, config.bot_token, config.owner_id, config.miniapp_url), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, config.miniapp_host, config.miniapp_port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info("Mini App слушает %s:%s", config.miniapp_host, config.miniapp_port)
    return runner
