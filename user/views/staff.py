from datetime import timedelta

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from user.models import CustomerNotification, Queue, User
from user.services.notifications import enqueue_queue_email

from .auth import get_json_body
from .queue import queue_to_dict
from user.services.queue import queue_position


GRACE_PERIOD = timedelta(minutes=2)
ACTIVE_STATUSES = ("called", "serving")


def get_staff_user(request):
    """Return the active database staff account held by this session."""
    if not request.session.get("is_logged_in") or request.session.get("user_role") != "staff":
        return None
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        return User.objects.get(pk=user_id, role="staff", is_active=True)
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return None


def _staff_queue_data(entry):
    data = queue_to_dict(entry)
    position, people_ahead, estimated_wait = queue_position(entry)
    data.update(
        {
            "position": position,
            "people_ahead": people_ahead,
            "estimated_wait": f"{estimated_wait} min",
            "called_by_id": entry.called_by_id,
            "called_by": entry.called_by.username if entry.called_by_id else None,
            "called_at": entry.called_at.isoformat() if entry.called_at else None,
            "grace_deadline": (
                entry.grace_deadline.isoformat() if entry.grace_deadline else None
            ),
            "can_skip": bool(
                entry.status == "called"
                and entry.grace_deadline
                and entry.grace_deadline <= timezone.now()
            ),
        }
    )
    return data


def _get_queue_id(request):
    data = get_json_body(request)
    if not isinstance(data, dict):
        return None, JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )
    queue_id = data.get("queue_id")
    if not isinstance(queue_id, (int, str)) or not str(queue_id).isdigit():
        return None, JsonResponse(
            {"success": False, "message": "A valid queue ID is required."}, status=400
        )
    return int(queue_id), None


def _staff_denied():
    return JsonResponse(
        {"success": False, "message": "Active staff login required."}, status=403
    )


@require_http_methods(["GET"])
def staff_queue_api(request):
    user_id = request.session.get("user_id")
    role = request.session.get("user_role")
    if not request.session.get("is_logged_in") or role not in ("staff", "admin") or not user_id:
        return _staff_denied()
    try:
        current_user = User.objects.get(pk=user_id, role=role, is_active=True)
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return _staff_denied()

    queue_qs = (
        Queue.objects.filter(status__in=("waiting", "called", "serving"))
        .select_related("user", "called_by")
        .order_by("created_at", "pk")
    )
    queue_list = [_staff_queue_data(entry) for entry in queue_qs]
    current_qs = Queue.objects.filter(status__in=ACTIVE_STATUSES).select_related(
        "user", "called_by"
    )
    if current_user.role == "staff":
        current_qs = current_qs.filter(called_by=current_user)
    current_entries = list(current_qs.order_by("created_at", "pk"))
    current_serving = _staff_queue_data(current_entries[0]) if current_entries else None
    return JsonResponse(
        {
            "success": True,
            "queues": queue_list,
            "waiting_count": Queue.objects.filter(status="waiting").count(),
            "completed_count": Queue.objects.filter(status="completed").count(),
            "current_serving": current_serving,
            "my_calls": [_staff_queue_data(entry) for entry in current_entries],
            "current_user_id": current_user.id,
        }
    )


@require_http_methods(["GET"])
def staff_history_api(request):
    user_id = request.session.get("user_id")
    role = request.session.get("user_role")
    if not request.session.get("is_logged_in") or role not in ("staff", "admin") or not user_id:
        return _staff_denied()
    try:
        User.objects.get(pk=user_id, role=role, is_active=True)
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return _staff_denied()
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(100, max(1, int(request.GET.get("limit", 20))))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Invalid history page."}, status=400)
    history = Queue.objects.filter(status__in=("completed", "cancelled", "skipped"))
    records = list(history.select_related("user", "called_by").order_by("-created_at", "-pk")[offset:offset + limit])
    return JsonResponse({
        "success": True,
        "records": [_staff_queue_data(entry) for entry in records],
        "next_offset": offset + len(records),
        "has_more": history.count() > offset + len(records),
    })


@csrf_exempt
@require_http_methods(["POST"])
def call_next_api(request):
    staff = get_staff_user(request)
    if not staff:
        return _staff_denied()

    try:
        with transaction.atomic():
            # Serialize calls by the same staff member, while allowing other staff
            # members to claim different waiting entries at the same time.
            User.objects.select_for_update().get(pk=staff.pk)
            if Queue.objects.filter(called_by=staff, status__in=ACTIVE_STATUSES).exists():
                return JsonResponse(
                    {"success": False, "message": "You already have a called or serving customer."},
                    status=409,
                )
            entry = (
                Queue.objects.select_for_update(skip_locked=True)
                .filter(status="waiting")
                .order_by("created_at", "pk")
                .first()
            )
            if entry is None:
                return JsonResponse({"success": False, "message": "No waiting customer."}, status=404)

            if entry.token_number is None:
                return JsonResponse(
                    {
                        "success": False,
                        "message": "This queue entry has no token. Tokens are assigned when customers join.",
                    },
                    status=409,
                )

            now = timezone.now()
            entry.status = "called"
            entry.called_by = staff
            entry.called_at = now
            entry.grace_deadline = now + GRACE_PERIOD
            entry.save(
                update_fields=[
                    "status",
                    "called_by",
                    "called_at",
                    "grace_deadline",
                    "updated_at",
                ]
            )
            enqueue_queue_email(
                entry,
                "called",
                "Your turn is called",
                f"Token {entry.token} has been called. Please proceed to the counter.",
            )
    except User.DoesNotExist:
        return _staff_denied()

    CustomerNotification.objects.create(user=entry.user, title="Your turn is called", message=f"Token {entry.token} has been called. Please proceed to the counter.")

    return JsonResponse(
        {"success": True, "message": "Next customer called successfully.", "queue": _staff_queue_data(entry)}
    )


@csrf_exempt
@require_http_methods(["POST"])
def start_service_api(request):
    staff = get_staff_user(request)
    if not staff:
        return _staff_denied()
    queue_id, error = _get_queue_id(request)
    if error:
        return error
    data = get_json_body(request)
    duration = data.get("service_duration_minutes") if isinstance(data, dict) else None
    if duration is not None and (type(duration) is not int or duration < 1):
        return JsonResponse(
            {"success": False, "message": "Expected service duration must be a positive number of minutes."},
            status=400,
        )
    with transaction.atomic():
        try:
            entry = Queue.objects.select_for_update().select_related("user", "called_by").get(pk=queue_id)
        except Queue.DoesNotExist:
            return JsonResponse({"success": False, "message": "Queue entry not found."}, status=404)
        if entry.called_by_id != staff.pk:
            return JsonResponse({"success": False, "message": "You can only start service for a customer assigned to you."}, status=403)
        if entry.status != "called":
            return JsonResponse({"success": False, "message": "Only a called customer can start service."}, status=409)
        entry.status = "serving"
        update_fields = ["status", "updated_at"]
        if duration is not None:
            entry.service_duration_minutes = duration
            update_fields.append("service_duration_minutes")
        entry.save(update_fields=update_fields)
        title = "Service started"
        message = f"Service for token {entry.token} has started."
        CustomerNotification.objects.create(user=entry.user, title=title, message=message)
        enqueue_queue_email(entry, "started", title, message)
    return JsonResponse({"success": True, "message": "Service started.", "queue": _staff_queue_data(entry)})


@csrf_exempt
@require_http_methods(["POST"])
def complete_token_api(request):
    staff = get_staff_user(request)
    if not staff:
        return _staff_denied()
    queue_id, error = _get_queue_id(request)
    if error:
        return error
    with transaction.atomic():
        try:
            entry = Queue.objects.select_for_update().select_related("user", "called_by").get(pk=queue_id)
        except Queue.DoesNotExist:
            return JsonResponse({"success": False, "message": "Queue entry not found."}, status=404)
        if entry.called_by_id != staff.pk:
            return JsonResponse({"success": False, "message": "You can only complete a customer assigned to you."}, status=403)
        if entry.status != "serving":
            return JsonResponse({"success": False, "message": "Start service before completing this customer."}, status=409)
        entry.status = "completed"
        entry.save(update_fields=["status", "updated_at"])
        title = "Service completed"
        message = f"Service for token {entry.token} is complete."
        CustomerNotification.objects.create(user=entry.user, title=title, message=message)
        enqueue_queue_email(entry, "completed", title, message)
    return JsonResponse({"success": True, "message": "Customer service completed.", "queue": _staff_queue_data(entry)})


@csrf_exempt
@require_http_methods(["POST"])
def skip_token_api(request):
    staff = get_staff_user(request)
    if not staff:
        return _staff_denied()
    queue_id, error = _get_queue_id(request)
    if error:
        return error
    with transaction.atomic():
        try:
            entry = Queue.objects.select_for_update().select_related("user", "called_by").get(pk=queue_id)
        except Queue.DoesNotExist:
            return JsonResponse({"success": False, "message": "Queue entry not found."}, status=404)
        if entry.called_by_id != staff.pk:
            return JsonResponse({"success": False, "message": "You can only skip a customer assigned to you."}, status=403)
        if entry.status != "called":
            return JsonResponse({"success": False, "message": "Only a called customer may be skipped."}, status=409)
        if entry.grace_deadline is None or timezone.now() < entry.grace_deadline:
            return JsonResponse(
                {"success": False, "message": "The two-minute grace period has not elapsed."},
                status=409,
            )
        entry.status = "skipped"
        entry.save(update_fields=["status", "updated_at"])
        title = "Queue entry skipped"
        message = f"Token {entry.token} was marked skipped."
        CustomerNotification.objects.create(user=entry.user, title=title, message=message)
        enqueue_queue_email(entry, "skipped", title, message)
    return JsonResponse({"success": True, "message": "Customer skipped successfully.", "queue": _staff_queue_data(entry)})
