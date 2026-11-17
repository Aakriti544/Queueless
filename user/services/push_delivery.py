"""Background Web Push delivery for per-device queue notification events."""

import json
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from user.models import PushSubscription, Queue, QueuePushDelivery
from user.services.push import decrypt_subscription, push_is_configured


MAX_PUSH_ATTEMPTS = 3
PUSH_RETRY_BASE = timedelta(minutes=1)
PUSH_DELIVERY_LEASE = timedelta(minutes=2)


def send_web_push(subscription_info, payload):
    """Send a small encrypted payload with VAPID authentication."""
    from pywebpush import webpush

    return webpush(
        subscription_info=subscription_info,
        data=json.dumps(payload, separators=(",", ":")),
        vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY,
        vapid_claims={"sub": settings.WEB_PUSH_VAPID_SUBJECT},
        ttl=120,
        timeout=10,
    )


def _claim_push_delivery(now):
    with transaction.atomic():
        QueuePushDelivery.objects.filter(
            status=QueuePushDelivery.STATUS_SENDING,
            lease_expires_at__lte=now,
            attempt_count__gte=MAX_PUSH_ATTEMPTS,
        ).update(
            status=QueuePushDelivery.STATUS_FAILED,
            lease_expires_at=None,
            last_error="Push worker lease expired after the maximum attempts.",
            updated_at=now,
        )
        delivery = (
            QueuePushDelivery.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=QueuePushDelivery.STATUS_PENDING, next_attempt_at__lte=now)
                | Q(
                    status=QueuePushDelivery.STATUS_SENDING,
                    lease_expires_at__lte=now,
                    attempt_count__lt=MAX_PUSH_ATTEMPTS,
                )
            )
            .order_by("next_attempt_at", "pk")
            .first()
        )
        if delivery is None:
            return None
        delivery.status = QueuePushDelivery.STATUS_SENDING
        delivery.attempt_count += 1
        delivery.lease_expires_at = now + PUSH_DELIVERY_LEASE
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
        subscription = PushSubscription.objects.filter(
            pk=delivery.subscription_id, is_active=True
        ).first()
        return {
            "id": delivery.pk,
            "subscription_id": delivery.subscription_id,
            "encrypted_subscription": (
                subscription.encrypted_subscription if subscription else None
            ),
            "title": delivery.title,
            "body": delivery.body,
            "url": delivery.url,
            "attempt_count": delivery.attempt_count,
            "event": delivery.queue_event.event,
            "queue_id": delivery.queue_event.queue_id,
        }


def _http_status(error):
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _expire_subscription(delivery_id, subscription_id):
    with transaction.atomic():
        now = timezone.now()
        QueuePushDelivery.objects.filter(
            subscription_id=subscription_id,
            status__in=(QueuePushDelivery.STATUS_PENDING, QueuePushDelivery.STATUS_SENDING),
        ).exclude(pk=delivery_id).update(
            status=QueuePushDelivery.STATUS_FAILED,
            lease_expires_at=None,
            last_error="Push subscription expired or was rejected by its push service.",
            updated_at=now,
        )
        PushSubscription.objects.filter(pk=subscription_id).delete()
        QueuePushDelivery.objects.filter(pk=delivery_id).update(
            status=QueuePushDelivery.STATUS_FAILED,
            lease_expires_at=None,
            last_error="Push subscription expired or was rejected by its push service.",
            updated_at=now,
        )


def process_pending_queue_pushes(batch_size=25, now=None):
    """Deliver due pushes without holding database locks during network I/O."""
    current_time = now or timezone.now()
    outcomes = {"processed": 0, "sent": 0, "retrying": 0, "failed": 0, "expired": 0}

    for _ in range(max(1, batch_size)):
        delivery = _claim_push_delivery(current_time)
        if delivery is None:
            break
        outcomes["processed"] += 1
        if (
            delivery["event"]
            in {"estimate-change", "periodic", "approaching", "next", "three-ahead"}
            and not Queue.objects.filter(pk=delivery["queue_id"], status="waiting").exists()
        ):
            QueuePushDelivery.objects.filter(pk=delivery["id"]).update(
                status=QueuePushDelivery.STATUS_FAILED,
                lease_expires_at=None,
                last_error="Waiting reminder cancelled because the queue entry is no longer waiting.",
                updated_at=timezone.now(),
            )
            outcomes["failed"] += 1
            continue
        if not delivery["encrypted_subscription"]:
            QueuePushDelivery.objects.filter(pk=delivery["id"]).update(
                status=QueuePushDelivery.STATUS_FAILED,
                lease_expires_at=None,
                last_error="Push subscription is no longer active.",
                updated_at=timezone.now(),
            )
            outcomes["failed"] += 1
            continue
        try:
            if not push_is_configured():
                raise RuntimeError("Web Push credentials are not configured.")
            subscription_info = decrypt_subscription(
                delivery["encrypted_subscription"]
            )
            send_web_push(
                subscription_info,
                {
                    "title": delivery["title"],
                    "body": delivery["body"],
                    "url": delivery["url"],
                },
            )
        except Exception as error:
            if _http_status(error) in (400, 404, 410) and delivery["subscription_id"]:
                _expire_subscription(delivery["id"], delivery["subscription_id"])
                outcomes["expired"] += 1
                continue
            retry_at = current_time + PUSH_RETRY_BASE * (
                2 ** (delivery["attempt_count"] - 1)
            )
            terminal = delivery["attempt_count"] >= MAX_PUSH_ATTEMPTS
            # Exception strings can contain the endpoint capability URL. Store
            # only its type and HTTP code, never the provider response or URL.
            code = _http_status(error)
            safe_error = (
                f"Web Push delivery failed (HTTP {code})."
                if code
                else f"Web Push delivery failed ({type(error).__name__})."
            )
            QueuePushDelivery.objects.filter(
                pk=delivery["id"], status=QueuePushDelivery.STATUS_SENDING
            ).update(
                status=(
                    QueuePushDelivery.STATUS_FAILED
                    if terminal
                    else QueuePushDelivery.STATUS_PENDING
                ),
                next_attempt_at=retry_at,
                lease_expires_at=None,
                last_error=safe_error,
                updated_at=timezone.now(),
            )
            outcomes["failed" if terminal else "retrying"] += 1
        else:
            QueuePushDelivery.objects.filter(
                pk=delivery["id"], status=QueuePushDelivery.STATUS_SENDING
            ).update(
                status=QueuePushDelivery.STATUS_SENT,
                sent_at=timezone.now(),
                lease_expires_at=None,
                last_error="",
                updated_at=timezone.now(),
            )
            outcomes["sent"] += 1
    return outcomes
