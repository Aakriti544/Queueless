"""Scheduled, deduplicated waiting-time reminders for active queue entries."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from user.models import CustomerNotification, Queue, QueueWaitNotice
from user.services.notifications import enqueue_queue_email
from user.services.queue import queue_position


SIGNIFICANT_WAIT_CHANGE_MINUTES = 5
REMINDER_INTERVAL_MINUTES = 5


def _queue_event_key(queue_id, event, slot=""):
    suffix = f":{slot}" if slot else ""
    return f"queue:{queue_id}:wait:{event}{suffix}"


def _enqueue_notice(queue, event, key, title, message, wait_minutes, people_ahead):
    """Persist in-app, email and enabled-device push jobs atomically."""
    notice, created = QueueWaitNotice.objects.get_or_create(
        event_key=key,
        defaults={
            "queue": queue,
            "event": event,
            "estimated_wait_minutes": max(0, wait_minutes),
            "people_ahead": max(0, people_ahead),
        },
    )
    if not created:
        return False
    CustomerNotification.objects.create(
        user=queue.user, title=title, message=message
    )
    enqueue_queue_email(
        queue, event[:32], title, message, event_key=key
    )
    return True


def process_waiting_notifications(batch_size=100, now=None):
    """Schedule due notices without performing network delivery in this worker."""
    current_time = now or timezone.now()
    result = {"processed": 0, "notices": 0}
    queryset = (
        Queue.objects.filter(status="waiting", active_customer__isnull=False)
        .select_related("user", "called_by")
        .order_by("created_at", "pk")
    )
    for queue_id in queryset.values_list("pk", flat=True)[: max(1, batch_size)]:
        with transaction.atomic():
            queue = (
                Queue.objects.select_for_update(skip_locked=True)
                .select_related("user", "called_by")
                .filter(pk=queue_id, status="waiting", active_customer__isnull=False)
                .first()
            )
            if queue is None:
                continue
            result["processed"] += 1
            _, people_ahead, wait_minutes = queue_position(queue, now=current_time)

            baseline_key = _queue_event_key(queue.pk, "baseline")
            baseline, _ = QueueWaitNotice.objects.get_or_create(
                event_key=baseline_key,
                defaults={
                    "queue": queue,
                    "event": "baseline",
                    "estimated_wait_minutes": wait_minutes,
                    "people_ahead": people_ahead,
                },
            )

            if people_ahead == 3:
                message = (
                    f"There are exactly 3 people ahead of token {queue.token or '—'} "
                    "in the queue."
                )
                if _enqueue_notice(
                    queue,
                    "three-ahead",
                    _queue_event_key(queue.pk, "three-ahead"),
                    "3 people ahead",
                    message,
                    wait_minutes,
                    people_ahead,
                ):
                    result["notices"] += 1

            # Notify a material estimate shift at most once in each five-minute
            # window; compare with the latest significant shift or join baseline.
            last_change = (
                QueueWaitNotice.objects.filter(queue=queue, event="estimate-change")
                .order_by("-created_at", "-pk")
                .first()
            ) or baseline
            if (
                abs(wait_minutes - last_change.estimated_wait_minutes)
                >= SIGNIFICANT_WAIT_CHANGE_MINUTES
                and current_time - last_change.created_at >= timedelta(minutes=5)
            ):
                slot = int(current_time.timestamp() // 300)
                message = (
                    f"Your estimated wait for token {queue.token or '—'} is now "
                    f"about {wait_minutes} minutes. This is an estimate and is not guaranteed."
                )
                if _enqueue_notice(
                    queue, "estimate-change", _queue_event_key(queue.pk, "change", slot),
                    "Waiting time updated", message, wait_minutes, people_ahead
                ):
                    result["notices"] += 1

            elapsed_seconds = max(0, (current_time - queue.created_at).total_seconds())
            reminder_slot = int(elapsed_seconds // (REMINDER_INTERVAL_MINUTES * 60))
            if reminder_slot > 0:
                message = (
                    f"Token {queue.token or '—'} is still waiting. There are "
                    f"{people_ahead} customers ahead and the estimated wait is about "
                    f"{wait_minutes} minutes. Estimated times are not guaranteed."
                )
                if _enqueue_notice(
                    queue, "periodic", _queue_event_key(queue.pk, "five-minute", reminder_slot),
                    "Queue waiting update", message, wait_minutes, people_ahead
                ):
                    result["notices"] += 1

            if 0 < wait_minutes <= 10:
                message = (
                    f"Your estimated wait for token {queue.token or '—'} is about "
                    f"{wait_minutes} minutes. Please stay nearby; this estimate is not guaranteed."
                )
                if _enqueue_notice(
                    queue, "approaching", _queue_event_key(queue.pk, "approaching"),
                    "Your turn is approaching", message, wait_minutes, people_ahead
                ):
                    result["notices"] += 1

            if people_ahead <= 1:
                message = (
                    f"Token {queue.token or '—'} is next or very close to being called. "
                    "Please stay nearby."
                )
                if _enqueue_notice(
                    queue, "next", _queue_event_key(queue.pk, "next"),
                    "You are nearly next", message, wait_minutes, people_ahead
                ):
                    result["notices"] += 1
    return result
