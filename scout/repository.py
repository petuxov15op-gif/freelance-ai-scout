from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .core import normalize_link, similar, text_hash
from .db import transaction
from .models import (AIUsage, Analysis, Channel, ChannelCandidate, Feedback, Job,
                     JobSource, Message, MonitoringState, Notification, SearchQuery, Setting, utcnow)


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
                if query is None and channel.status in {"rejected", "removed"}:
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

    def monitored_channels(self) -> list[Channel]:
        with self.factory() as db:
            return list(db.scalars(select(Channel).where(Channel.status.in_(("approved", "paused"))).order_by(Channel.id)))

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
                state = db.get(MonitoringState, channel_id)
                if state is None:
                    db.add(MonitoringState(channel_id=channel_id, last_message_id=initial_cursor))
                else:
                    state.last_message_id = max(state.last_message_id, initial_cursor)
            return True

    def set_monitoring(self, channel_id: int, enabled: bool, latest_cursor: int = 0) -> bool:
        if latest_cursor < 0:
            raise ValueError("Курсор сообщения не может быть отрицательным")
        with transaction(self.factory) as db:
            channel = db.get(Channel, channel_id)
            expected = "paused" if enabled else "approved"
            if channel is None or channel.status != expected:
                return False
            if enabled:
                state = db.get(MonitoringState, channel_id)
                if state is None:
                    state = MonitoringState(channel_id=channel_id, last_message_id=latest_cursor)
                    db.add(state)
                elif latest_cursor > state.last_message_id:
                    state.last_message_id = latest_cursor
                    state.checked_at = utcnow()
            else:
                db.execute(update(Message).where(Message.channel_id == channel_id, Message.status == "pending")
                           .values(status="irrelevant", rejection_reason="channel_paused"))
            channel.status = "approved" if enabled else "paused"
            return True

    def remove_source(self, channel_id: int) -> bool:
        """Stop monitoring without deleting its messages, jobs or cursor."""
        with transaction(self.factory) as db:
            channel = db.get(Channel, channel_id)
            if channel is None or channel.status not in {"approved", "paused"}:
                return False
            db.execute(update(Message).where(Message.channel_id == channel_id, Message.status == "pending")
                       .values(status="irrelevant", rejection_reason="channel_paused"))
            channel.status = "removed"
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

    def mark_message(self, message_id: int, status: str, reason: str | None = None) -> None:
        with transaction(self.factory) as db:
            message = db.get(Message, message_id)
            message.status = status
            message.rejection_reason = reason

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
                identity = (message.metadata_json or {}).get("contact_url") or f"channel:{message.channel_id}"
                canonical_hash = hashlib.sha256((message.text_hash + "|" + identity).encode()).hexdigest()
                match = Job(canonical_hash=canonical_hash)
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
        exact = db.execute(select(Job, Message).join(JobSource, JobSource.job_id == Job.id)
                           .join(Message, Message.id == JobSource.message_id)
                           .where(Message.text_hash == message.text_hash).limit(200)).all()
        for job, other in exact:
            old_contact = (other.metadata_json or {}).get("contact_url")
            new_contact = (message.metadata_json or {}).get("contact_url")
            if (old_contact and old_contact == new_contact) or (not old_contact and not new_contact and other.channel_id == message.channel_id):
                return job
        recent = db.execute(select(Job, Message).join(JobSource, JobSource.job_id == Job.id)
                            .join(Message, Message.id == JobSource.message_id)
                            .order_by(Job.id.desc()).limit(200)).all()
        # Similarity alone merges independent customers using the same template.
        # Require a shared, confirmed contact when links differ.
        contact = (message.metadata_json or {}).get("contact_url")
        if not contact:
            return None
        return next((job for job, other in recent if (other.metadata_json or {}).get("contact_url") == contact
                     and similar(message.text, other.text)), None)

    def set_job_details(self, job_id: int, employment: str, contact: dict) -> None:
        with transaction(self.factory) as db:
            job = db.get(Job, job_id)
            job.employment_type = employment
            job.contact_status = contact["status"]
            job.contact_method = contact["method"]
            job.contact_url = contact.get("url")

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
            return db.scalar(select(Message.link).join(JobSource).join(Channel, Channel.id == Message.channel_id)
                             .where(JobSource.job_id == job_id)
                             .order_by(case((Channel.status == "approved", 0), else_=1), Message.id).limit(1))

    def job_original(self, job_id: int) -> str | None:
        with self.factory() as db:
            return db.scalar(select(Message.text).join(JobSource).where(JobSource.job_id == job_id).limit(1))

    def job_message(self, job_id: int) -> Message | None:
        with self.factory() as db:
            return db.scalar(select(Message).join(JobSource).where(JobSource.job_id == job_id).limit(1))

    def job_channel(self, job_id: int) -> Channel | None:
        with self.factory() as db:
            return db.scalar(select(Channel).join(Message).join(JobSource)
                             .where(JobSource.job_id == job_id)
                             .order_by(case((Channel.status == "approved", 0), else_=1), Message.id).limit(1))

    def jobs(self, minimum_score: int = 0, limit: int = 10) -> list[Job]:
        with self.factory() as db:
            return list(db.scalars(select(Job).where(Job.score >= minimum_score).order_by(Job.created_at.desc()).limit(limit)))

    def widget_jobs(self, limit: int = 3) -> list[Job]:
        with self.factory() as db:
            return list(db.scalars(select(Job).where(Job.score.is_not(None), Job.hidden.is_(False))
                                   .order_by(Job.created_at.desc()).limit(limit)))

    def set_widget_job_flag(self, job_id: int, flag: str, value: bool) -> bool:
        if flag not in {"saved", "hidden"}:
            raise ValueError("Недопустимое поле заказа")
        with transaction(self.factory) as db:
            job = db.get(Job, job_id)
            if job is None:
                return False
            setattr(job, flag, value)
            return True

    def widget_dashboard(self) -> dict[str, int]:
        since = utcnow() - timedelta(hours=24)
        with self.factory() as db:
            return {
                "new": db.scalar(select(func.count()).select_from(Job).where(Job.created_at >= since)) or 0,
                "matching": db.scalar(select(func.count()).select_from(Job).where(
                    Job.contact_status.in_(("direct_free", "platform_free")))) or 0,
                "saved": db.scalar(select(func.count()).select_from(Job).where(Job.saved.is_(True))) or 0,
                "applied": db.scalar(select(func.count()).select_from(Job).where(Job.applied_at.is_not(None))) or 0,
                "sources": db.scalar(select(func.count()).select_from(Channel).where(Channel.status == "approved")) or 0,
            }

    def widget_sources(self, page: int, size: int = 2) -> tuple[list[tuple[Channel, int, int]], int]:
        with self.factory() as db:
            total = db.scalar(select(func.count()).select_from(Channel).where(
                Channel.status.in_(("approved", "paused")))) or 0
            channels = list(db.scalars(select(Channel).where(Channel.status.in_(("approved", "paused")))
                                       .order_by(Channel.id).offset((page - 1) * size).limit(size)))
            rows = []
            for channel in channels:
                found = db.scalar(select(func.count()).select_from(Message).where(Message.channel_id == channel.id)) or 0
                matching = db.scalar(select(func.count(func.distinct(JobSource.job_id)))
                                     .join(Message, Message.id == JobSource.message_id)
                                     .where(Message.channel_id == channel.id)) or 0
                rows.append((channel, found, matching))
            return rows, total

    def jobs_needing_contact(self, limit: int = 5) -> list[Job]:
        with self.factory() as db:
            return list(db.scalars(select(Job).where(Job.score >= 75, Job.contact_status.in_(("unknown", "none")))
                                   .order_by(Job.score.desc(), Job.created_at.desc()).limit(limit)))

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
            job = db.get(Job, job_id)
            job.status = action
            job.hidden = action == "irrelevant"
            if action == "interesting":
                job.saved = True
            first_application = action == "applied" and job.applied_at is None
            first_reply = action == "replied" and job.replied_at is None
            if first_application:
                job.applied_at = utcnow()
            if first_reply:
                job.replied_at = utcnow()
            candidate = db.scalar(select(ChannelCandidate).join(Channel, Channel.id == ChannelCandidate.channel_id)
                                  .join(Message, Message.channel_id == Channel.id)
                                  .join(JobSource, JobSource.message_id == Message.id)
                                  .where(JobSource.job_id == job_id, ChannelCandidate.query.is_not(None)).limit(1))
            if candidate and (first_application or first_reply):
                query = db.scalar(select(SearchQuery).where(SearchQuery.phrase == candidate.query))
                if query:
                    field = "applications" if first_application else "replies"
                    setattr(query, field, getattr(query, field) + 1)

    def set_job_note(self, job_id: int, note: str) -> bool:
        with transaction(self.factory) as db:
            job = db.get(Job, job_id)
            if not job:
                return False
            job.note = note[:1000] or None
            return True

    def set_job_amount(self, job_id: int, amount: str) -> bool:
        with transaction(self.factory) as db:
            job = db.get(Job, job_id)
            if not job:
                return False
            job.amount = amount[:100] or None
            return True

    def search_queries(self, groups: set[str] | None = None) -> list[SearchQuery]:
        with self.factory() as db:
            query = select(SearchQuery).where(SearchQuery.enabled.is_(True))
            if groups is not None:
                query = query.where(SearchQuery.group.in_(groups))
            return list(db.scalars(query.order_by(SearchQuery.id)))

    def seed_search_queries(self, grouped: dict[str, tuple[str, ...]]) -> None:
        with transaction(self.factory) as db:
            existing = set(db.scalars(select(SearchQuery.phrase)))
            for group, phrases in grouped.items():
                for phrase in phrases:
                    if phrase not in existing:
                        db.add(SearchQuery(phrase=phrase, group=group))
                        existing.add(phrase)

    def record_search(self, phrase: str, **counts: int | str | None) -> None:
        with transaction(self.factory) as db:
            row = db.scalar(select(SearchQuery).where(SearchQuery.phrase == phrase))
            if not row:
                return
            for field in ("searches", "new_sources", "suitable_jobs", "duplicates"):
                setattr(row, field, getattr(row, field) + int(counts.get(field, 0) or 0))
            if counts.get("success"):
                row.last_success_at = utcnow()
                row.last_error = None
            if counts.get("error"):
                row.last_error = str(counts["error"])[:300]

    def analytics(self, days: int | None) -> dict:
        since = utcnow() - timedelta(days=days) if days else None
        with self.factory() as db:
            messages = select(func.count()).select_from(Message)
            jobs = select(func.count()).select_from(Job)
            unique = select(func.count()).select_from(Job).where(Job.score.is_not(None), Job.contact_status.in_(("direct_free", "platform_free")))
            if since:
                messages = messages.where(Message.discovered_at >= since)
                jobs = jobs.where(Job.created_at >= since)
                unique = unique.where(Job.created_at >= since)
            def action_ids(action):
                query = select(Feedback.job_id).where(Feedback.action == action)
                return query.where(Feedback.created_at >= since) if since else query
            def count_ids(query):
                return db.scalar(select(func.count(func.distinct(Job.id))).where(Job.id.in_(query))) or 0
            applied_ids = action_ids("applied")
            replied_ids = action_ids("replied")
            won_ids = action_ids("won")
            return {"found": db.scalar(messages) or 0, "jobs": db.scalar(jobs) or 0,
                    "unique": db.scalar(unique) or 0, "applied": count_ids(applied_ids),
                    "replied": count_ids(replied_ids), "won": count_ids(won_ids),
                    "replied_from_applied": db.scalar(select(func.count(func.distinct(Job.id))).where(
                        Job.id.in_(applied_ids), Job.id.in_(replied_ids))) or 0,
                    "won_from_applied": db.scalar(select(func.count(func.distinct(Job.id))).where(
                        Job.id.in_(applied_ids), Job.id.in_(won_ids))) or 0}

    def top_channels(self, limit: int = 5, days: int | None = None) -> list[tuple[str, int, int, int, int]]:
        with self.factory() as db:
            unique = func.count(func.distinct(JobSource.job_id))
            applied = func.count(func.distinct(case((Feedback.action == "applied", Job.id))))
            replied = func.count(func.distinct(case((Feedback.action == "replied", Job.id))))
            won = func.count(func.distinct(case((Feedback.action == "won", Job.id))))
            query = (select(Channel.username, unique, applied, replied, won)
                .join(Message, Message.channel_id == Channel.id)
                .join(JobSource, JobSource.message_id == Message.id)
                .join(Job, Job.id == JobSource.job_id)
                .outerjoin(Feedback, Feedback.job_id == Job.id)
                .where(Job.contact_status.in_(("direct_free", "platform_free")))
                .group_by(Channel.id).order_by((won * 10 + replied * 4 + applied * 2 + unique).desc()).limit(limit))
            if days:
                query = query.where(Job.created_at >= utcnow() - timedelta(days=days))
            return [(name or "?", count, apps, replies, wins) for name, count, apps, replies, wins in db.execute(query)]

    def top_queries(self, days: int | None = None, limit: int = 3) -> list[tuple[str, int]]:
        with self.factory() as db:
            unique = func.count(func.distinct(Job.id))
            query = (select(ChannelCandidate.query, unique)
                     .join(Channel, Channel.id == ChannelCandidate.channel_id)
                     .join(Message, Message.channel_id == Channel.id)
                     .join(JobSource, JobSource.message_id == Message.id)
                     .join(Job, Job.id == JobSource.job_id)
                     .where(ChannelCandidate.query.is_not(None), Job.contact_status.in_(("direct_free", "platform_free")))
                     .group_by(ChannelCandidate.query).order_by(unique.desc()).limit(limit))
            if days:
                query = query.where(Job.created_at >= utcnow() - timedelta(days=days))
            return list(db.execute(query))

    def rejection_counts(self, limit: int = 5) -> list[tuple[str, int]]:
        with self.factory() as db:
            return [(reason, count) for reason, count in db.execute(
                select(Message.rejection_reason, func.count()).where(Message.rejection_reason.is_not(None))
                .group_by(Message.rejection_reason).order_by(func.count().desc()).limit(limit))]

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
