"""Transactional outbox and retryable queue email delivery."""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from user.models import PushSubscription, Queue, QueueEmailDelivery, QueuePushDelivery
from user.services.brevo_email import send_brevo_email


MAX_EMAIL_ATTEMPTS = 3
EMAIL_RETRY_BASE = timedelta(minutes=1)
EMAIL_DELIVERY_LEASE = timedelta(minutes=2)


def enqueue_queue_email(queue, event, title, message, *, event_key=None):
    """Persist one email event; callers must invoke this in their queue transaction."""
    recipient = (queue.user.email or "").strip()
    defaults = {
        "user": queue.user,
        "queue": queue,
        "event": event,
        "recipient_email": recipient,
        "subject": f"QueueLess: {title}",
        "message": (
            f"Hello {queue.user.username},\n\n{message}\n\n"
            f"Token: {queue.token or '—'}\n"
            f"Service: {queue.service}\n\nQueueLess"
        ),
    }
    if not recipient:
        defaults.update(
            {
                "status": QueueEmailDelivery.STATUS_FAILED,
                "last_error": "Customer has no registered email address.",
            }
        )
    delivery, created = QueueEmailDelivery.objects.get_or_create(
        event_key=event_key or f"queue:{queue.pk}:{event}", defaults=defaults
    )
    if created:
        token = queue.token or "—"
        push_copy = {
            "joined": (
                "Queue joined",
                f"{token} · {queue.service}",
                "/queuestatus.html/",
            ),
            "three-ahead": (
                "3 people ahead",
                f"Token {token}: 3 people are ahead of you in the queue.",
                "/queuestatus.html/",
            ),
            "called": (
                "Your turn is called",
                f"Token {token} has been called. Please proceed to the counter.",
                "/queuestatus.html/",
            ),
            "cancelled": (
                "Queue cancelled",
                f"Token {token} was cancelled.",
                "/notifications.html/",
            ),
        }.get(
            event,
            (
                "QueueLess update",
                "Your queue status changed. Open QueueLess to view details.",
                "/notifications.html/",
            ),
        )
        for subscription in PushSubscription.objects.filter(
            user=queue.user, is_active=True
        ):
            QueuePushDelivery.objects.get_or_create(
                queue_event=delivery,
                subscription=subscription,
                defaults={
                    "title": push_copy[0],
                    "body": push_copy[1][:160],
                    "url": push_copy[2],
                },
            )
    return delivery


def _claim_email_delivery(now):
    with transaction.atomic():
        QueueEmailDelivery.objects.filter(
            status=QueueEmailDelivery.STATUS_SENDING,
            lease_expires_at__lte=now,
            attempt_count__gte=MAX_EMAIL_ATTEMPTS,
        ).update(
            status=QueueEmailDelivery.STATUS_FAILED,
            lease_expires_at=None,
            last_error="Delivery worker lease expired after the maximum attempts.",
            updated_at=now,
        )
        delivery = (
            QueueEmailDelivery.objects.select_for_update(skip_locked=True)
            .filter(
                Q(
                    status=QueueEmailDelivery.STATUS_PENDING,
                    next_attempt_at__lte=now,
                )
                | Q(
                    status=QueueEmailDelivery.STATUS_SENDING,
                    lease_expires_at__lte=now,
                    attempt_count__lt=MAX_EMAIL_ATTEMPTS,
                )
            )
            .order_by("next_attempt_at", "pk")
            .first()
        )
        if delivery is None:
            return None
        delivery.status = QueueEmailDelivery.STATUS_SENDING
        delivery.attempt_count += 1
        delivery.lease_expires_at = now + EMAIL_DELIVERY_LEASE
        delivery.last_error = ""
        delivery.save(
            update_fields=[
                "status",
                "attempt_count",
                "lease_expires_at",
                "last_error",
                "updated_at",
            ]
        )
        return {
            "id": delivery.pk,
            "recipient": delivery.recipient_email,
            "subject": delivery.subject,
            "message": delivery.message,
            "attempt_count": delivery.attempt_count,
            "queue_id": delivery.queue_id,
            "event": delivery.event,
        }


def process_pending_queue_emails(batch_size=25, now=None):
    """Attempt due emails without holding database locks during delivery."""
    current_time = now or timezone.now()
    outcomes = {"processed": 0, "sent": 0, "retrying": 0, "failed": 0}

    for _ in range(max(1, batch_size)):
        delivery = _claim_email_delivery(current_time)
        if delivery is None:
            break
        outcomes["processed"] += 1
        if (
            delivery["event"]
            in {"estimate-change", "periodic", "approaching", "next", "three-ahead"}
            and not Queue.objects.filter(pk=delivery["queue_id"], status="waiting").exists()
        ):
            QueueEmailDelivery.objects.filter(pk=delivery["id"]).update(
                status=QueueEmailDelivery.STATUS_FAILED,
                lease_expires_at=None,
                last_error="Waiting reminder cancelled because the queue entry is no longer waiting.",
                updated_at=timezone.now(),
            )
            outcomes["failed"] += 1
            continue
        try:
            send_brevo_email(
                recipient=delivery["recipient"],
                subject=delivery["subject"],
                text_content=delivery["message"],
            )
        except Exception as error:
            retry_at = current_time + EMAIL_RETRY_BASE * (2 ** (delivery["attempt_count"] - 1))
            terminal = delivery["attempt_count"] >= MAX_EMAIL_ATTEMPTS
            QueueEmailDelivery.objects.filter(
                pk=delivery["id"], status=QueueEmailDelivery.STATUS_SENDING
            ).update(
                status=(
                    QueueEmailDelivery.STATUS_FAILED
                    if terminal
                    else QueueEmailDelivery.STATUS_PENDING
                ),
                next_attempt_at=retry_at,
                lease_expires_at=None,
                last_error=str(error)[:4000],
                updated_at=timezone.now(),
            )
            outcomes["failed" if terminal else "retrying"] += 1
        else:
            QueueEmailDelivery.objects.filter(
                pk=delivery["id"], status=QueueEmailDelivery.STATUS_SENDING
            ).update(
                status=QueueEmailDelivery.STATUS_SENT,
                sent_at=timezone.now(),
                lease_expires_at=None,
                last_error="",
                updated_at=timezone.now(),
            )
            outcomes["sent"] += 1
    return outcomes
