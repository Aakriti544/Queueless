from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from user.models import PushSubscription, QueuePushDelivery
from user.services.push import (
    encrypt_subscription,
    endpoint_fingerprint,
    push_configuration_status,
    push_is_configured,
    validate_subscription,
)

from .auth import get_json_body
from .queue import _customer_for_request


@require_http_methods(["GET", "POST", "DELETE"])
def customer_push_subscriptions_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error

    if request.method == "GET":
        configuration = push_configuration_status()
        configured = configuration["configured"]
        return JsonResponse(
            {
                "success": True,
                "configured": configured,
                "configuration": configuration,
                "vapid_public_key": (
                    settings_public_key() if configured else None
                ),
                "device_count": PushSubscription.objects.filter(
                    user=customer, is_active=True
                ).count(),
            }
        )

    data = get_json_body(request)
    if not isinstance(data, dict):
        return JsonResponse({"success": False, "message": "Invalid JSON data."}, status=400)

    if request.method == "POST":
        if not push_is_configured():
            return JsonResponse(
                {"success": False, "message": "Push notifications are not configured."},
                status=503,
            )
        try:
            subscription_data = validate_subscription(data.get("subscription"))
            encrypted = encrypt_subscription(subscription_data)
        except (ValidationError, ImproperlyConfigured) as exception:
            message = (
                exception.messages[0]
                if isinstance(exception, ValidationError)
                else "Push notification security is not configured correctly."
            )
            return JsonResponse({"success": False, "message": message}, status=400)

        fingerprint = endpoint_fingerprint(subscription_data["endpoint"])
        try:
            with transaction.atomic():
                subscription, created = PushSubscription.objects.select_for_update().get_or_create(
                    endpoint_hash=fingerprint,
                    defaults={
                        "user": customer,
                        "encrypted_subscription": encrypted,
                        "is_active": True,
                    },
                )
                if not created:
                    if subscription.user_id != customer.pk:
                        QueuePushDelivery.objects.filter(
                            subscription=subscription,
                            status__in=(
                                QueuePushDelivery.STATUS_PENDING,
                                QueuePushDelivery.STATUS_SENDING,
                            ),
                        ).update(
                            status=QueuePushDelivery.STATUS_FAILED,
                            lease_expires_at=None,
                            last_error="Push subscription was reassigned to another account.",
                        )
                        subscription.user = customer
                    subscription.encrypted_subscription = encrypted
                    subscription.is_active = True
                    subscription.save(
                        update_fields=["user", "encrypted_subscription", "is_active", "updated_at"]
                    )
        except IntegrityError:
            return JsonResponse(
                {"success": False, "message": "Unable to save this push subscription."},
                status=409,
            )
        return JsonResponse(
            {
                "success": True,
                "enabled": True,
                "device_count": PushSubscription.objects.filter(
                    user=customer, is_active=True
                ).count(),
            },
            status=201 if created else 200,
        )

    try:
        subscription_data = validate_subscription(data.get("subscription"))
    except ValidationError:
        return JsonResponse({"success": False, "message": "Invalid push subscription."}, status=400)
    fingerprint = endpoint_fingerprint(subscription_data["endpoint"])
    with transaction.atomic():
        subscription = PushSubscription.objects.select_for_update().filter(
            user=customer, endpoint_hash=fingerprint
        ).first()
        if subscription:
            QueuePushDelivery.objects.filter(
                subscription=subscription,
                status__in=(
                    QueuePushDelivery.STATUS_PENDING,
                    QueuePushDelivery.STATUS_SENDING,
                ),
            ).update(
                status=QueuePushDelivery.STATUS_FAILED,
                lease_expires_at=None,
                last_error="Push subscription was disabled by the customer.",
            )
            subscription.delete()
    return JsonResponse(
        {
            "success": True,
            "removed": bool(subscription),
            "device_count": PushSubscription.objects.filter(
                user=customer, is_active=True
            ).count(),
        }
    )


def settings_public_key():
    from django.conf import settings

    return settings.WEB_PUSH_VAPID_PUBLIC_KEY
