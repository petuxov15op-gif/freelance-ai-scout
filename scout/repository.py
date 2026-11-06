from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .core import normalize_link, similar, text_hash
from .db import transaction
from .models import (AIUsage, Analysis, Channel, ChannelCandidate, Feedback, Job,
                     JobSource, Message, MonitoringState, Notification, Setting, utcnow)


class Repository:
    def __init__(self, factory: sessionmaker):
        self.factory = factory

    def seed(self) -> None:
        with transaction(self.factory) as db:
            if not db.get(Setting, "instant_notifications_start_at"):
                db.add(Setting(key="instant_notifications_start_at", value=utcnow().isoformat()))

    def setting(self, key: str, default: str = "") -> str:
        with self.factory() as db:
            row = db.get(Setting, key)
            return row.value if row else default

    def set_setting(self, key: str, value: str) -> None:
        with transaction(self.factory) as db:
            row = db.get(Setting, key)
            if row:
                row.value = value
            else:
                db.add(Setting(key=key, value=value))

    def candidate(self, telegram_id: int, username: str | None, title: str, description: str | None,
                  query: str | None, usefulness: int, reason: str, examples: list[str]) -> Channel | None:
        with transaction(self.factory) as db:
            channel = db.scalar(select(Channel).where(Channel.telegram_id == telegram_id))
            if channel:
                if query is None and channel.status == "rejected":
                    channel.status = "pending"
                    channel.reviewed_at = None
                    channel.username = username
                    channel.title = title[:255]
                    channel.description = description
                    candidate = db.scalar(select(ChannelCandidate).where(ChannelCandidate.channel_id == channel.id))
                    if candidate:
                        candidate.query = None
                        candidate.usefulness = usefulness
                        candidate.reason = reason
                        candidate.examples = examples[:5]
                        candidate.owner_notified = False
                    return channel
                return None
            channel = Channel(telegram_id=telegram_id, username=username, title=title[:255], description=description, status="pending")
            db.add(channel)
            db.flush()
            db.add(ChannelCandidate(channel_id=channel.id, query=query, usefulness=usefulness,
                                    reason=reason, examples=examples[:5]))
            return channel

    def list_channels(self, status: str) -> list[Channel]:
        with self.factory() as db:
            return list(db.scalars(select(Channel).where(Channel.status == status).order_by(Channel.id)))

    def get_channel(self, channel_id: int) -> Channel | None:
        with self.factory() as db:
            return db.get(Channel, channel_id)

    def get_candidate(self, channel_id: int) -> ChannelCandidate | None:
        with self.factory() as db:
            return db.scalar(select(ChannelCandidate).where(ChannelCandidate.channel_id == channel_id))

    def mark_candidate_notified(self, channel_id: int) -> None:
        with transaction(self.factory) as db:
            candidate = db.scalar(select(ChannelCandidate).where(ChannelCandidate.channel_id == channel_id))
            if candidate:
                candidate.owner_notified = True

    def transition_channel(self, channel_id: int, status: str, initial_cursor: int = 0) -> bool:
        if status not in {"approved", "rejected", "blocked"}:
            raise ValueError("Недопустимый статус")
        if initial_cursor < 0:
            raise ValueError("Курсор сообщения не может быть отрицательным")
        with transaction(self.factory) as db:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.status != "pending":
                return False
            channel.status = status
            channel.reviewed_at = utcnow()
            if status == "approved":
                db.add(MonitoringState(channel_id=channel_id, last_message_id=initial_cursor))
            return True

    def cursor(self, channel_id: int) -> int:
        with self.factory() as db:
            state = db.get(MonitoringState, channel_id)
            return state.last_message_id if state else 0

    def advance_cursor(self, channel_id: int, message_id: int) -> None:
        with transaction(self.factory) as db:
            state = db.get(MonitoringState, channel_id)
            if state and message_id > state.last_message_id:
                state.last_message_id = message_id
                state.checked_at = utcnow()

    def save_message(self, channel_id: int, telegram_message_id: int, text: str, link: str | None,
                     published_at: datetime, metadata: dict, advance_cursor: bool = True) -> Message:
        with transaction(self.factory) as db:
            message = db.scalar(select(Message).where(Message.channel_id == channel_id,
                                                      Message.telegram_message_id == telegram_message_id))
            if message is None:
                message = Message(channel_id=channel_id, telegram_message_id=telegram_message_id,
                                  text=text, link=link, normalized_link=normalize_link(link), text_hash=text_hash(text),
                                  published_at=published_at, metadata_json=metadata)
                db.add(message)
                db.flush()
            state = db.get(MonitoringState, channel_id)
            if advance_cursor and state and telegram_message_id > state.last_message_id:
                state.last_message_id = telegram_message_id
                state.checked_at = utcnow()
            return message

    def mark_message(self, message_id: int, status: str) -> None:
        with transaction(self.factory) as db:
            db.get(Message, message_id).status = status

    def pending_messages(self, limit: int | None = 500) -> list[Message]:
        with self.factory() as db:
            query = select(Message).where(Message.status == "pending").order_by(Message.id)
            return list(db.scalars(query.limit(limit) if limit is not None else query))

    def previously_irrelevant(self, message: Message) -> bool:
        with self.factory() as db:
            return bool(db.scalar(select(Message.id).where(Message.text_hash == message.text_hash,
                             Message.status == "irrelevant", Message.id != message.id).limit(1)))

    def attach_job(self, message: Message) -> tuple[Job, bool]:
        with transaction(self.factory) as db:
            existing_source = db.scalar(select(JobSource).where(JobSource.message_id == message.id))
            if existing_source:
                return db.get(Job, existing_source.job_id), False
            match = self._match_job(db, message)
            created = match is None
            if created:
                match = Job(canonical_hash=message.text_hash)
                db.add(match)
                db.flush()
            db.add(JobSource(job_id=match.id, message_id=message.id))
            return match, created

    def matching_job(self, message: Message) -> Job | None:
        with self.factory() as db:
            return self._match_job(db, message)

    @staticmethod
    def _match_job(db, message: Message) -> Job | None:
        if message.normalized_link:
            match = db.scalar(select(Job).join(JobSource).join(Message).where(
                Message.normalized_link == message.normalized_link).limit(1))
            if match:
                return match
        match = db.scalar(select(Job).where(Job.canonical_hash == message.text_hash))
        if match:
            return match
        recent = db.execute(select(Job, Message).join(JobSource, JobSource.job_id == Job.id)
                            .join(Message, Message.id == JobSource.message_id)
                            .order_by(Job.id.desc()).limit(200)).all()
        return next((job for job, other in recent if similar(message.text, other.text)), None)

    def save_analysis(self, job_id: int, kind: str, model: str, result: dict) -> None:
        with transaction(self.factory) as db:
            existing = db.scalar(select(Analysis).where(Analysis.job_id == job_id, Analysis.kind == kind, Analysis.model == model))
            if existing:
                existing.result = result
            else:
                db.add(Analysis(job_id=job_id, kind=kind, model=model, result=result))
            if kind == "deep":
                job = db.get(Job, job_id)
                job.title = result["title"][:255]
                job.score = result["score"]

    def analysis(self, job_id: int, kind: str = "deep") -> Analysis | None:
        with self.factory() as db:
            return db.scalar(select(Analysis).where(Analysis.job_id == job_id, Analysis.kind == kind).order_by(Analysis.id.desc()))

    def job(self, job_id: int) -> Job | None:
        with self.factory() as db:
            return db.get(Job, job_id)

    def job_link(self, job_id: int) -> str | None:
        with self.factory() as db:
            return db.scalar(select(Message.link).join(JobSource).where(JobSource.job_id == job_id).limit(1))

    def job_original(self, job_id: int) -> str | None:
        with self.factory() as db:
            return db.scalar(select(Message.text).join(JobSource).where(JobSource.job_id == job_id).limit(1))

    def job_message(self, job_id: int) -> Message | None:
        with self.factory() as db:
            return db.scalar(select(Message).join(JobSource).where(JobSource.job_id == job_id).limit(1))

    def job_channel(self, job_id: int) -> Channel | None:
        with self.factory() as db:
            return db.scalar(select(Channel).join(Message).join(JobSource)
                             .where(JobSource.job_id == job_id).limit(1))

    def jobs(self, minimum_score: int = 0, limit: int = 10) -> list[Job]:
        with self.factory() as db:
            return list(db.scalars(select(Job).where(Job.score >= minimum_score).order_by(Job.created_at.desc()).limit(limit)))

    def pending_instant_jobs(self, minimum_score: int) -> list[Job]:
        started_at = datetime.fromisoformat(self.setting("instant_notifications_start_at"))
        with self.factory() as db:
            return list(db.scalars(select(Job).where(Job.score >= minimum_score, Job.created_at >= started_at,
                     ~Job.id.in_(select(Notification.job_id).where(Notification.kind == "instant"))).limit(100)))

    def claim_notification(self, job_id: int, kind: str) -> bool:
        try:
            with transaction(self.factory) as db:
                db.add(Notification(job_id=job_id, kind=kind))
            return True
        except IntegrityError:
            return False

    def finish_notification(self, job_id: int, kind: str, telegram_message_id: int) -> None:
        with transaction(self.factory) as db:
            note = db.scalar(select(Notification).where(Notification.job_id == job_id, Notification.kind == kind))
            note.telegram_message_id = telegram_message_id
            note.sent_at = utcnow()

    def release_notification(self, job_id: int, kind: str) -> None:
        with transaction(self.factory) as db:
            note = db.scalar(select(Notification).where(Notification.job_id == job_id, Notification.kind == kind))
            if note and note.sent_at is None:
                db.delete(note)

    def recover_stale_notifications(self) -> None:
        cutoff = utcnow() - timedelta(minutes=10)
        with transaction(self.factory) as db:
            stale = db.scalars(select(Notification).where(Notification.sent_at.is_(None), Notification.created_at < cutoff)).all()
            for note in stale:
                db.delete(note)

    def add_feedback(self, job_id: int, action: str) -> None:
        with transaction(self.factory) as db:
            db.add(Feedback(job_id=job_id, action=action))
            db.get(Job, job_id).status = action

    def record_usage(self, model: str, purpose: str, usage: dict) -> None:
        with transaction(self.factory) as db:
            db.add(AIUsage(model=model, purpose=purpose, prompt_tokens=usage.get("prompt_tokens"),
                           completion_tokens=usage.get("completion_tokens"), actual_cost_usd=usage.get("cost")))

    def cost_today(self) -> float:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.factory() as db:
            return float(db.scalar(select(func.coalesce(func.sum(AIUsage.actual_cost_usd), 0)).where(AIUsage.created_at >= today)) or 0)

    def stats(self) -> dict:
        with self.factory() as db:
            return {"approved": db.scalar(select(func.count()).select_from(Channel).where(Channel.status == "approved")),
                    "pending": db.scalar(select(func.count()).select_from(Channel).where(Channel.status == "pending")),
                    "messages": db.scalar(select(func.count()).select_from(Message)),
                    "jobs": db.scalar(select(func.count()).select_from(Job))}

    def health_activity(self) -> dict:
        since = utcnow() - timedelta(hours=24)
        with self.factory() as db:
            return {
                "messages": db.scalar(select(func.count()).select_from(Message).where(Message.discovered_at >= since)),
                "notifications": db.scalar(select(func.count()).select_from(Notification).where(Notification.sent_at >= since)),
                "last_notification": db.scalar(select(func.max(Notification.sent_at))),
            }
