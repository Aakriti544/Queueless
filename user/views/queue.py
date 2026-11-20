import logging
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from user.models import CustomerNotification, Organization, Queue, QueueWaitNotice, Service, User
from user.services.queue import get_active_queue, join_shared_queue, queue_position
from user.services.notifications import enqueue_queue_email

from .auth import get_json_body

logger = logging.getLogger(__name__)


def queue_to_dict(queue):
    return {
        "id": queue.id,
        "username": queue.user.username,
        "email": queue.user.email,
        "service": queue.service,
        "service_duration_minutes": queue.service_duration_minutes,
        "token": queue.token,
        "token_number": queue.token_number,
        "status": queue.status,
        "created_at": queue.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": queue.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _customer_for_request(request):
    if not request.session.get("is_logged_in") or not request.session.get("user_id"):
        return None, JsonResponse(
            {"success": False, "message": "Please login first."}, status=401
        )
    if request.session.get("user_role") != "customer":
        return None, JsonResponse(
            {"success": False, "message": "Customer access required."}, status=403
        )
    try:
        customer = User.objects.get(
            pk=request.session["user_id"], role="customer", is_active=True
        )
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return None, JsonResponse(
            {"success": False, "message": "Customer session is invalid."}, status=401
        )
    return customer, None


def _queue_response(queue, message, created):
    position, people_ahead, estimated_wait = queue_position(queue)
    updated_at = timezone.localtime(timezone.now())
    expected_turn_at = updated_at + timedelta(minutes=estimated_wait)
    queue_data = {
        "id": queue.id,
        "token": queue.token,
        "token_number": queue.token_number,
        "service": queue.service,
        "position": position,
        "peopleAhead": people_ahead,
        "estimatedWait": f"{estimated_wait} min",
        "estimatedWaitMinutes": estimated_wait,
        "estimatedTurnAt": expected_turn_at.isoformat() if queue.status == "waiting" else None,
        "lastUpdatedAt": updated_at.isoformat(),
        "status": queue.status,
        "organization": _organization_data(),
        "created_at": queue.created_at.isoformat(),
    }
    return JsonResponse(
        {
            "success": True,
            "message": message,
            "has_queue": True,
            "token": queue.token,
            "token_number": queue.token_number,
            "position": position,
            "people_ahead": people_ahead,
            "estimated_wait": f"{estimated_wait} min",
            "status": queue.status,
            "queue": queue_data,
        },
        status=201 if created else 200,
    )


@csrf_exempt
@require_http_methods(["POST"])
def join_queue_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    data = get_json_body(request)
    if not isinstance(data, dict):
        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )
    service_id = data.get("service_id")
    if type(service_id) is not int:
        return JsonResponse(
            {"success": False, "message": "A valid service is required."}, status=400
        )
    service_obj = Service.objects.filter(pk=service_id, is_active=True).first()
    if service_obj is None:
        return JsonResponse({"success": False, "message": "This service is unavailable."}, status=400)
    service = service_obj.name
    try:
        with transaction.atomic():
            queue, created = join_shared_queue(customer, service)
            if created:
                title = "Queue joined"
                _, people_ahead, initial_wait = queue_position(queue)
                message = (
                    f"Your token {queue.token} has been added to the {service} queue. "
                    f"Your initial estimated wait is about {initial_wait} minutes. "
                    "Estimated times are not guaranteed."
                )
                QueueWaitNotice.objects.get_or_create(
                    event_key=f"queue:{queue.pk}:wait:baseline",
                    defaults={
                        "queue": queue,
                        "event": "baseline",
                        "estimated_wait_minutes": initial_wait,
                        "people_ahead": people_ahead,
                    },
                )
                CustomerNotification.objects.create(
                    user=customer, title=title, message=message
                )
                enqueue_queue_email(queue, "joined", title, message)
    except User.DoesNotExist:
        request.session.flush()
        return JsonResponse(
            {"success": False, "message": "Customer account is inactive."}, status=401
        )
    except IntegrityError:
        # The OneToOne active-customer constraint is the final race-condition guard.
        existing = get_active_queue(customer)
        if existing:
            return _queue_response(existing, "You are already in a queue.", False)
        return JsonResponse(
            {"success": False, "message": "Unable to join the queue. Please retry."},
            status=409,
        )
    message = (
        "You joined the queue successfully."
        if created
        else "You are already in a queue."
    )
    return _queue_response(queue, message, created)


@require_http_methods(["GET"])
def my_queue_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    queue = get_active_queue(customer)
    if queue is None:
        return JsonResponse(
            {
                "success": True,
                "has_queue": False,
                "message": "You are not currently in a queue.",
            }
        )
    position, people_ahead, estimated_wait = queue_position(queue)
    updated_at = timezone.localtime(timezone.now())
    expected_turn_at = updated_at + timedelta(minutes=estimated_wait)
    return JsonResponse(
        {
            "success": True,
            "has_queue": True,
            "queue": {
                "id": queue.id,
                "token": queue.token,
                "token_number": queue.token_number,
                "service": queue.service,
                "position": position,
                "peopleAhead": people_ahead,
                "estimatedWait": f"{estimated_wait} min",
                "estimatedWaitMinutes": estimated_wait,
                "estimatedTurnAt": expected_turn_at.isoformat() if queue.status == "waiting" else None,
                "lastUpdatedAt": updated_at.isoformat(),
                "status": queue.status,
                "organization": _organization_data(),
                "created_at": queue.created_at.isoformat(),
            },
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def leave_queue_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    with transaction.atomic():
        queue = (
            Queue.objects.select_for_update()
            .filter(active_customer=customer)
            .select_related("user")
            .first()
        )
        if queue is None:
            return JsonResponse(
                {"success": False, "message": "You are not currently in a queue."},
                status=404,
            )
        queue.status = "cancelled"
        queue.save(update_fields=["status", "updated_at"])
        title = "Queue cancelled"
        message = f"Your token {queue.token or '—'} was cancelled."
        CustomerNotification.objects.create(user=customer, title=title, message=message)
        enqueue_queue_email(queue, "cancelled", title, message)
    return JsonResponse(
        {"success": True, "message": "You have left the queue.", "token": queue.token}
    )


@require_http_methods(["GET"])
def customer_services_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    services = Service.objects.filter(is_active=True).order_by("name")
    service_list = [
        {"id": service.id, "name": service.name, "description": service.description}
        for service in services
    ]
    return JsonResponse(
        {"success": True, "organization": _organization_data(), "services": service_list, "count": len(service_list)}
    )


def _organization_data():
    organization = Organization.objects.first()
    if not organization:
        return None
    return {"id": organization.pk, "name": organization.name, "email": organization.email, "phone": organization.phone, "address": organization.address}


@require_http_methods(["GET"])
def customer_history_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(100, max(1, int(request.GET.get("limit", 20))))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Invalid history page."}, status=400)
    try:
        queryset = Queue.objects.filter(
            user=customer, status__in=("completed", "cancelled", "skipped")
        ).order_by("-created_at", "-pk")
        page = list(queryset[offset:offset + limit + 1])
        has_more = len(page) > limit
        records = page[:limit]
    except DatabaseError:
        logger.exception("Customer queue history query failed for customer %s", customer.pk)
        return JsonResponse(
            {
                "success": False,
                "message": "Queue history is temporarily unavailable. Check the database connection and applied migrations.",
            },
            status=503,
        )
    return JsonResponse({"success": True, "records": [{"id": q.pk, "token": q.token, "service": q.service, "status": q.status, "created_at": q.created_at.isoformat()} for q in records], "next_offset": offset + len(records), "has_more": has_more})


@require_http_methods(["GET", "PATCH"])
def customer_profile_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    if request.method == "PATCH":
        data = get_json_body(request)
        if not isinstance(data, dict):
            return JsonResponse({"success": False, "message": "Invalid JSON data."}, status=400)
        username = data.get("username")
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 150:
            return JsonResponse({"success": False, "message": "Enter a name up to 150 characters."}, status=400)
        customer.username = username.strip()
        customer.save(update_fields=["username", "updated_at"])
        request.session["username"] = customer.username
    return JsonResponse({"success": True, "authenticated": True, "user": {"id": customer.pk, "username": customer.username, "email": customer.email, "role": customer.role}})


@require_http_methods(["GET", "POST"])
def customer_notifications_api(request):
    customer, error = _customer_for_request(request)
    if error:
        return error
    if request.method == "POST":
        data = get_json_body(request)
        ids = data.get("ids") if isinstance(data, dict) else None
        qs = CustomerNotification.objects.filter(user=customer, is_read=False)
        if isinstance(ids, list):
            qs = qs.filter(pk__in=[i for i in ids if isinstance(i, int)])
        qs.update(is_read=True)
    notes = CustomerNotification.objects.filter(user=customer)
    try:
        after_id = max(0, int(request.GET.get("after_id", 0)))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Invalid notification cursor."}, status=400)
    response_notes = notes.filter(pk__gt=after_id).order_by("pk") if after_id else notes
    notification_rows = response_notes.values(
        "id", "title", "message", "created_at", "is_read"
    )[:100]
    response_data = {
        "success": True,
        "notifications": [
            {**item, "created_at": item["created_at"].isoformat()}
            for item in notification_rows
        ],
    }
    if request.GET.get("include_unread_ids") == "1":
        unread_ids = list(
            notes.filter(is_read=False).values_list("pk", flat=True)
        )
        response_data["unread_ids"] = unread_ids
        response_data["unread_count"] = len(unread_ids)
    elif request.GET.get("include_unread_count") != "0":
        response_data["unread_count"] = notes.filter(is_read=False).count()
    return JsonResponse(response_data)


@require_http_methods(["GET"])
def notification_unread_count_api(request):
    """Return the signed-in account's unread count, scoped to its own user row."""
    if not request.session.get("is_logged_in") or not request.session.get("user_id"):
        return JsonResponse({"success": False, "message": "Please login first."}, status=401)
    try:
        account = User.objects.get(
            pk=request.session["user_id"],
            is_active=True,
            role=request.session.get("user_role"),
        )
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return JsonResponse({"success": False, "message": "User session is invalid."}, status=401)
    unread_count = CustomerNotification.objects.filter(user=account, is_read=False).count()
    return JsonResponse({"success": True, "unread_count": unread_count})
