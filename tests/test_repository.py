from datetime import datetime, timezone

from scout.models import Channel, Message
from scout.repository import Repository


def test_channel_approval_and_rejection(repo):
    first = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "заказы", ["Нужен бот"])
    assert first.status == "pending"
    assert repo.transition_channel(first.id, "approved", initial_cursor=42)
    assert repo.cursor(first.id) == 42
    assert not repo.transition_channel(first.id, "rejected")
    assert repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", []) is None
    second = repo.candidate(200, "scoutjobs2", "Jobs 2", "", None, 0, "", [])
    assert repo.transition_channel(second.id, "rejected")
    assert second.id not in [c.id for c in repo.list_channels("approved")]
    assert repo.candidate(200, "scoutjobs2", "Jobs 2", "", "automatic", 0, "", []) is None
    assert repo.candidate(200, "scoutjobs2", "Jobs 2", "", None, 0, "ручная проверка", []) is not None


def test_messages_cursor_and_duplicates(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    now = datetime.now(timezone.utc)
    a = repo.save_message(channel.id, 1, "Нужен сайт для компании, оплата по договоренности", "https://t.me/scoutjobs/1", now, {})
    again = repo.save_message(channel.id, 1, a.text, a.link, now, {})
    assert a.id == again.id
    assert repo.cursor(channel.id) == 1
    b = repo.save_message(channel.id, 2, a.text, "https://t.me/scoutjobs/2", now, {})
    job_a, created_a = repo.attach_job(a)
    job_b, created_b = repo.attach_job(b)
    assert created_a and not created_b and job_a.id == job_b.id
    assert repo.cursor(channel.id) == 2
    assert len(repo.pending_messages()) == 2


def test_notification_claim(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    message = repo.save_message(channel.id, 1, "Нужен сайт для компании, оплата по договоренности", None, datetime.now(timezone.utc), {})
    job, _ = repo.attach_job(message)
    assert repo.claim_notification(job.id, "instant")
    assert not repo.claim_notification(job.id, "instant")
    repo.finish_notification(job.id, "instant", 42)
    repo.release_notification(job.id, "instant")
    assert not repo.claim_notification(job.id, "instant")


def test_old_jobs_are_not_queued_when_notification_threshold_changes(repo):
    channel = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    repo.transition_channel(channel.id, "approved")
    message = repo.save_message(channel.id, 1, "Нужен сайт для компании, оплата по договоренности", None,
                                datetime.now(timezone.utc), {})
    job, _ = repo.attach_job(message)
    repo.set_setting("instant_notifications_start_at", datetime.now(timezone.utc).isoformat())
    repo.save_analysis(job.id, "deep", "test-model", {"title": "Сайт", "score": 70})
    assert repo.pending_instant_jobs(60) == []


def test_irrelevant_result_is_cached_by_text(repo):
    first = repo.candidate(100, "scoutjobs", "Jobs", "", None, 70, "", [])
    second = repo.candidate(200, "scoutjobs2", "Jobs 2", "", None, 70, "", [])
    repo.transition_channel(first.id, "approved")
    repo.transition_channel(second.id, "approved")
    now = datetime.now(timezone.utc)
    original = repo.save_message(first.id, 1, "Бесплатная реклама и продажа курса по продвижению каналов", None, now, {})
    repo.mark_message(original.id, "irrelevant")
    repeated = repo.save_message(second.id, 1, original.text, None, now, {})
    assert repo.previously_irrelevant(repeated)
