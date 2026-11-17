from functools import wraps

from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from user.models import Organization, Queue, Service, User

from .auth import get_json_body


QUEUE_HISTORY_STATUSES = {"completed", "cancelled", "skipped"}


def _admin_access_error(request):
    """Verify the session and the current database account for admin access."""
    if not request.session.get("is_logged_in") or not request.session.get("user_id"):
        return JsonResponse(
            {"success": False, "message": "Login required."}, status=401
        )

    try:
        user = User.objects.get(pk=request.session["user_id"])
    except (User.DoesNotExist, ValueError, TypeError):
        request.session.flush()
        return JsonResponse(
            {"success": False, "message": "User session is invalid."}, status=401
        )

    if not user.is_active:
        request.session.flush()
        return JsonResponse(
            {"success": False, "message": "This account is inactive."}, status=401
        )

    if user.role != "admin" or request.session.get("user_role") != "admin":
        return JsonResponse(
            {"success": False, "message": "Admin access required."}, status=403
        )

    return None


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        error = _admin_access_error(request)
        if error:
            return error
        return view_func(request, *args, **kwargs)

    return wrapped


def is_admin(request):
    return _admin_access_error(request) is None


def _json_object(request):
    data = get_json_body(request)
    return data if isinstance(data, dict) else None


def _string_value(data, key, current="", max_length=None):
    value = data.get(key, current)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{key} must be no more than {max_length} characters.")
    return value


@ensure_csrf_cookie
def admin_dashboard(request):
    error = _admin_access_error(request)
    if error:
        return redirect("login")
    user = User.objects.get(pk=request.session["user_id"])
    return render(request, "admindashboard.html", {"user": user})


@require_http_methods(["GET"])
@admin_required
def admin_customers_api(request):
    customers = User.objects.filter(role="customer").order_by("-created_at")
    customer_list = [
        {
            "id": customer.id,
            "username": customer.username,
            "email": customer.email,
            "role": customer.role,
            "is_active": customer.is_active,
            "created_at": customer.created_at.isoformat(),
        }
        for customer in customers
    ]
    return JsonResponse(
        {"success": True, "customers": customer_list, "count": len(customer_list)}
    )


@require_http_methods(["PATCH"])
@admin_required
def set_customer_active_api(request, customer_id):
    return _set_user_active(request, customer_id, "customer")


@require_http_methods(["GET"])
@admin_required
def admin_staff_api(request):
    staff_users = User.objects.filter(role="staff").order_by("-created_at")
    staff_list = [
        {
            "id": staff.id,
            "username": staff.username,
            "email": staff.email,
            "role": staff.role,
            "is_active": staff.is_active,
            "created_at": staff.created_at.isoformat(),
        }
        for staff in staff_users
    ]
    return JsonResponse(
        {"success": True, "staff": staff_list, "count": len(staff_list)}
    )


def _set_user_active(request, user_id, role, forced_active=None):
    data = _json_object(request)
    if data is None:
        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )
    is_active = forced_active if forced_active is not None else data.get("is_active")
    if not isinstance(is_active, bool):
        return JsonResponse(
            {"success": False, "message": "is_active must be true or false."},
            status=400,
        )
    try:
        user = User.objects.get(pk=user_id, role=role)
    except User.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": f"{role.title()} account not found."},
            status=404,
        )
    user.is_active = is_active
    user.save(update_fields=["is_active", "updated_at"])
    action = "activated" if is_active else "deactivated"
    return JsonResponse(
        {
            "success": True,
            "message": f"{role.title()} account {action} successfully.",
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
            },
        }
    )


@require_http_methods(["PATCH"])
@admin_required
def set_staff_active_api(request, staff_id):
    return _set_user_active(request, staff_id, "staff")


@require_http_methods(["DELETE"])
@admin_required
def delete_staff_api(request, staff_id):
    try:
        staff = User.objects.get(pk=staff_id, role="staff")
    except User.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Staff account not found."},
            status=404,
        )

    # Queue.user is CASCADE. Refuse deletion if this account owns queue rows so
    # no queue history or token records can be removed with the account.
    if Queue.objects.filter(user=staff).exists():
        return JsonResponse(
            {
                "success": False,
                "message": "This staff account has queue records and cannot be deleted without removing queue history.",
            },
            status=409,
        )

    try:
        staff.delete()
    except ProtectedError:
        return JsonResponse(
            {"success": False, "message": "This staff account is still referenced by protected records."},
            status=409,
        )
    except DatabaseError:
        return JsonResponse(
            {"success": False, "message": "Unable to delete the staff account. Please try again."},
            status=500,
        )

    return JsonResponse(
        {"success": True, "message": "Staff account deleted successfully."}
    )


@require_http_methods(["POST"])
@admin_required
def create_staff_api(request):
    data = _json_object(request)
    if data is None:
        return JsonResponse(
            {"success": False, "message": "Invalid request data."}, status=400
        )

    try:
        username = _string_value(data, "username", max_length=150)
        email = _string_value(data, "email", max_length=254).lower()
        password = data.get("password")
        if not isinstance(password, str):
            raise ValueError("Password is required.")
        if not password:
            raise ValueError("Password is required.")
        if not username:
            raise ValueError("Staff name is required.")
        if not email:
            raise ValueError("Email is required.")
        EmailValidator()(email)
    except ValidationError as exc:
        return JsonResponse(
            {"success": False, "message": " ".join(exc.messages)}, status=400
        )
    except ValueError as exc:
        return JsonResponse(
            {"success": False, "message": str(exc)}, status=400
        )

    if User.objects.filter(email__iexact=email).exists():
        return JsonResponse(
            {"success": False, "message": "This email is already registered."},
            status=409,
        )

    try:
        with transaction.atomic():
            staff = User.objects.create(
                username=username,
                email=email,
                password=make_password(password),
                role="staff",
                is_active=True,
            )
    except IntegrityError:
        return JsonResponse(
            {"success": False, "message": "This email is already registered."},
            status=409,
        )

    return JsonResponse(
        {
            "success": True,
            "message": "Staff account created successfully.",
            "staff": {
                "id": staff.id,
                "username": staff.username,
                "email": staff.email,
                "role": staff.role,
            },
        },
        status=201,
    )


@require_http_methods(["GET"])
@admin_required
def admin_queue_history_api(request, status):
    if status not in QUEUE_HISTORY_STATUSES:
        return JsonResponse(
            {"success": False, "message": "Unknown queue history status."},
            status=404,
        )
    queues = (
        Queue.objects.filter(status=status)
        .select_related("user", "called_by")
        .order_by("-created_at")
    )
    records = [
        {
            "id": entry.id,
            "token": entry.token,
            "token_number": entry.token_number,
            "customer": entry.user.username,
            "customer_email": entry.user.email,
            "customer_active": entry.user.is_active,
            "service": entry.service,
            "status": entry.status,
            "called_by": entry.called_by.username if entry.called_by_id else None,
            "called_by_active": entry.called_by.is_active if entry.called_by_id else None,
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        }
        for entry in queues
    ]
    return JsonResponse(
        {"success": True, "status": status, "count": len(records), "records": records}
    )


@ensure_csrf_cookie
@require_http_methods(["GET"])
@admin_required
def get_organization_api(request):
    organization, _ = Organization.objects.get_or_create(
        pk=1, defaults={"name": "QueueLess"}
    )
    return JsonResponse(
        {
            "success": True,
            "organization": {
                "name": organization.name,
                "email": organization.email,
                "phone": organization.phone,
                "address": organization.address,
            },
        }
    )


@require_http_methods(["PUT"])
@admin_required
def update_organization_api(request):
    data = _json_object(request)
    if data is None:
        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )
    organization, _ = Organization.objects.get_or_create(
        pk=1, defaults={"name": "QueueLess"}
    )
    try:
        name = _string_value(data, "name", organization.name, 200)
        email = _string_value(data, "email", organization.email, 254)
        phone = _string_value(data, "phone", organization.phone, 20)
        address = _string_value(data, "address", organization.address, 255)
        if not name:
            raise ValueError("Organization name cannot be empty.")
        if email:
            EmailValidator()(email)
    except (ValueError, ValidationError) as exc:
        message = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({"success": False, "message": message}, status=400)

    organization.name = name
    organization.email = email
    organization.phone = phone
    organization.address = address
    organization.save()
    return JsonResponse(
        {
            "success": True,
            "message": "Organization details updated successfully.",
            "organization": {
                "name": organization.name,
                "email": organization.email,
                "phone": organization.phone,
                "address": organization.address,
            },
        }
    )


@require_http_methods(["GET"])
@admin_required
def get_services_api(request):
    services = Service.objects.all()
    data = [
        {
            "id": service.id,
            "name": service.name,
            "description": service.description,
            "is_active": service.is_active,
            "created_at": service.created_at.isoformat(),
            "updated_at": service.updated_at.isoformat(),
        }
        for service in services
    ]
    return JsonResponse({"success": True, "total": len(data), "services": data})


@require_http_methods(["POST"])
@admin_required
def create_service_api(request):
    data = _json_object(request)
    if data is None:
        return JsonResponse({"success": False, "message": "Invalid JSON data."}, status=400)
    try:
        name = _string_value(data, "name", max_length=150)
        description = _string_value(data, "description", max_length=None)
        if not name:
            return JsonResponse({"success": False, "message": "Service name is required."}, status=400)
        if Service.objects.filter(name__iexact=name).exists():
            return JsonResponse({"success": False, "message": "A service with this name already exists."}, status=409)
        with transaction.atomic():
            service = Service.objects.create(name=name, description=description)
    except ValueError as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=400)
    except IntegrityError:
        return JsonResponse({"success": False, "message": "A service with this name already exists."}, status=409)
    return JsonResponse(
        {"success": True, "message": "Service created successfully.", "service": {"id": service.id, "name": service.name, "description": service.description, "is_active": service.is_active}},
        status=201,
    )


@require_http_methods(["PUT"])
@admin_required
def update_service_api(request, service_id):
    try:
        service = Service.objects.get(pk=service_id)
    except Service.DoesNotExist:
        return JsonResponse({"success": False, "message": "Service not found."}, status=404)
    data = _json_object(request)
    if data is None:
        return JsonResponse({"success": False, "message": "Invalid JSON data."}, status=400)
    try:
        name = _string_value(data, "name", service.name, 150)
        description = _string_value(data, "description", service.description)
        if not name:
            return JsonResponse({"success": False, "message": "Service name cannot be empty."}, status=400)
        if Service.objects.filter(name__iexact=name).exclude(pk=service.pk).exists():
            return JsonResponse({"success": False, "message": "Another service already uses this name."}, status=409)
        service.name = name
        service.description = description
        service.save()
    except ValueError as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=400)
    return JsonResponse({"success": True, "message": "Service updated successfully.", "service": {"id": service.id, "name": service.name, "description": service.description, "is_active": service.is_active}})


@require_http_methods(["PATCH"])
@admin_required
def toggle_service_api(request, service_id):
    data = _json_object(request)
    if data is None:
        return JsonResponse({"success": False, "message": "Invalid JSON data."}, status=400)
    is_active = data.get("is_active")
    if not isinstance(is_active, bool):
        return JsonResponse({"success": False, "message": "is_active must be true or false."}, status=400)
    try:
        service = Service.objects.get(pk=service_id)
    except Service.DoesNotExist:
        return JsonResponse({"success": False, "message": "Service not found."}, status=404)
    service.is_active = is_active
    service.save(update_fields=["is_active", "updated_at"])
    action = "activated" if is_active else "deactivated"
    return JsonResponse({"success": True, "message": f"Service {action} successfully.", "service": {"id": service.id, "name": service.name, "is_active": service.is_active}})


@require_http_methods(["DELETE"])
@admin_required
def delete_service_api(request, service_id):
    try:
        service = Service.objects.get(pk=service_id)
    except Service.DoesNotExist:
        return JsonResponse({"success": False, "message": "Service not found."}, status=404)
    if Queue.objects.filter(service__iexact=service.name).exists():
        return JsonResponse({"success": False, "message": "This service is already used in queue records. Deactivate it instead of deleting it."}, status=409)
    service.delete()
    return JsonResponse({"success": True, "message": "Service deleted successfully."})
