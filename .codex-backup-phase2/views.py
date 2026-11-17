import json
import logging
import random
import smtplib

from django.conf import settings

from django.contrib.auth.hashers import make_password, check_password

from django.core.mail import send_mail

from django.db import transaction

from django.http import JsonResponse

from django.shortcuts import render

from django.utils import timezone

from django.views.decorators.csrf import csrf_exempt

from django.views.decorators.http import require_http_methods, require_POST

from .models import User, Queue, QueueTokenCounter, EmailOTP, Service
from user.services.queue import calculate_customer_wait_time

# ============================================================
# LOGGER - used so the REAL SMTP/email error always shows up
# in the Django terminal, even though the JSON response the
# browser receives stays generic (for security).
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# HELPER - SEND OTP EMAIL
# ============================================================
#
# Shared by signup_api() and resend_otp() so there is only ONE
# place that talks to Gmail SMTP. Raises the original exception
# back to the caller - callers decide what to tell the user.
# ============================================================


def send_otp_email(username, email, otp, is_resend=False):

    if is_resend:
        subject = "QueueLess New Verification Code"
        headline = "Your new QueueLess verification code is"
    else:
        subject = "QueueLess Email Verification"
        headline = "Your QueueLess verification code is"

    message = (
        f"Hello {username},\n\n"
        f"{headline}: {otp}\n\n"
        f"This OTP is valid for 5 minutes.\n\n"
        f"Do not share this code with anyone.\n\n"
        f"Thank you,\n"
        f"QueueLess Team"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


# ============================================================
# HELPER - TURN AN SMTP EXCEPTION INTO A SAFE USER MESSAGE
# ============================================================
#
# The FULL exception (with traceback) is always printed / logged
# to the Django terminal via logger.error(..., exc_info=True) in
# the callers below. This function only decides what short,
# non-sensitive message goes back to the browser.
# ============================================================


def describe_email_error(error):

    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            "Email server rejected the login credentials. "
            "Check EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in your "
            ".env file - EMAIL_HOST_PASSWORD must be a 16-character "
            "Gmail App Password, not your normal Gmail password."
        )

    if isinstance(
        error, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError)
    ):
        return (
            "Could not connect to the email server (smtp.gmail.com:587). "
            "Check your internet connection, firewall, or antivirus, "
            "then try again."
        )

    if isinstance(error, smtplib.SMTPException):
        return "The email server rejected the message. Please try again."

    return "Unable to send OTP. Please check your email configuration."


# ============================================================
# HELPER - READ JSON
# ============================================================


def get_json_body(request):
    """
    Safely read JSON data sent from frontend.
    """

    try:

        if not request.body:
            return {}

        return json.loads(request.body.decode("utf-8"))

    except (json.JSONDecodeError, UnicodeDecodeError):

        return None


# ============================================================
# HELPER - GET STAFF USER
# ============================================================


def get_staff_user(request):
    """
    Return currently logged-in staff user.
    """

    if not request.session.get("is_logged_in"):
        return None

    if request.session.get("user_role") != "staff":
        return None

    user_id = request.session.get("user_id")

    if not user_id:
        return None

    try:

        return User.objects.get(id=user_id, role="staff", is_active=True)

    except User.DoesNotExist:

        return None


# ============================================================
# HELPER - CONVERT QUEUE TO JSON
# ============================================================


def queue_to_dict(queue):

    return {
        "id": queue.id,
        "username": queue.user.username,
        "email": queue.user.email,
        "service": queue.service,
        "token": queue.token,
        "token_number": queue.token_number,
        "status": queue.status,
        "created_at": queue.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": queue.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# SIGNUP API
# ============================================================

# ============================================================
# SIGNUP API - SEND OTP
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def signup_api(request):

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    # --------------------------------------------------------
    # GET DATA
    # --------------------------------------------------------

    username = str(data.get("username", "")).strip()

    email = str(data.get("email", "")).strip().lower()

    password = str(data.get("password", ""))

    confirm_password = str(data.get("confirm_password", ""))

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if not username:

        return JsonResponse(
            {"success": False, "message": "Please enter your name."}, status=400
        )

    if not email:

        return JsonResponse(
            {"success": False, "message": "Please enter your email."}, status=400
        )

    if "@" not in email or "." not in email:

        return JsonResponse(
            {"success": False, "message": "Please enter a valid email address."},
            status=400,
        )

    if not password:

        return JsonResponse(
            {"success": False, "message": "Please enter your password."}, status=400
        )

    if len(password) < 6:

        return JsonResponse(
            {
                "success": False,
                "message": "Password must contain at least 6 characters.",
            },
            status=400,
        )

    if password != confirm_password:

        return JsonResponse(
            {"success": False, "message": "Passwords do not match."}, status=400
        )

    # --------------------------------------------------------
    # CHECK EXISTING EMAIL
    # --------------------------------------------------------

    if User.objects.filter(email=email).exists():

        return JsonResponse(
            {"success": False, "message": "An account with this email already exists."},
            status=409,
        )

    # --------------------------------------------------------
    # CHECK EXISTING USERNAME
    # --------------------------------------------------------

    if User.objects.filter(username=username).exists():

        return JsonResponse(
            {"success": False, "message": "This username is already taken."}, status=409
        )

    # --------------------------------------------------------
    # GENERATE OTP
    # --------------------------------------------------------

    otp = str(random.randint(100000, 999999))

    # --------------------------------------------------------
    # REMOVE OLD OTP
    # --------------------------------------------------------

    EmailOTP.objects.filter(email=email).delete()

    # --------------------------------------------------------
    # HASH PASSWORD
    # --------------------------------------------------------

    hashed_password = make_password(password)

    # --------------------------------------------------------
    # CREATE TEMPORARY OTP RECORD
    # --------------------------------------------------------

    EmailOTP.objects.create(
        email=email, username=username, password=hashed_password, otp=otp
    )

    # --------------------------------------------------------
    # SEND EMAIL
    # --------------------------------------------------------

    try:

        send_otp_email(username, email, otp)

    except Exception as error:

        # Full exception + traceback -> Django terminal.
        # This is the REAL reason ("535 Authentication failed",
        # "Connection refused", etc.) instead of the generic
        # message the browser sees.
        logger.error(
            "OTP email error while signing up %s: %s",
            email,
            error,
            exc_info=True,
        )

        EmailOTP.objects.filter(email=email).delete()

        return JsonResponse(
            {"success": False, "message": describe_email_error(error)}, status=500
        )

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "OTP sent successfully. Please check your email.",
            "email": email,
        },
        status=200,
    )


# ============================================================
# VERIFY OTP API
# ============================================================
#
# Called once the user types the 6-digit code from their email.
# The real User account is only created here, AFTER the OTP is
# confirmed - matching the temporary EmailOTP record created in
# signup_api() above.
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def verify_otp_api(request):

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    email = str(data.get("email", "")).strip().lower()

    otp = str(data.get("otp", "")).strip()

    if not email:

        return JsonResponse(
            {"success": False, "message": "Email is required."}, status=400
        )

    if not otp:

        return JsonResponse(
            {"success": False, "message": "OTP is required."}, status=400
        )

    # --------------------------------------------------------
    # FIND PENDING SIGNUP
    # --------------------------------------------------------

    otp_record = EmailOTP.objects.filter(email=email).order_by("-created_at").first()

    if not otp_record:

        return JsonResponse(
            {
                "success": False,
                "message": "Signup session not found. Please sign up again.",
            },
            status=404,
        )

    # --------------------------------------------------------
    # CHECK EXPIRY
    # --------------------------------------------------------

    if otp_record.is_expired():

        otp_record.delete()

        return JsonResponse(
            {"success": False, "message": "OTP has expired. Please request a new one."},
            status=400,
        )

    # --------------------------------------------------------
    # CHECK OTP VALUE
    # --------------------------------------------------------

    if otp_record.otp != otp:

        return JsonResponse(
            {"success": False, "message": "Invalid OTP. Please try again."}, status=400
        )

    # --------------------------------------------------------
    # PREVENT DUPLICATE ACCOUNTS (safety net)
    # --------------------------------------------------------

    if User.objects.filter(email=email).exists():

        otp_record.delete()

        return JsonResponse(
            {"success": False, "message": "An account with this email already exists."},
            status=409,
        )

    # --------------------------------------------------------
    # CREATE THE REAL ACCOUNT
    # --------------------------------------------------------
    # otp_record.password is already hashed (make_password was
    # called back in signup_api), so it is stored as-is here.
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            user = User.objects.create(
                username=otp_record.username,
                email=email,
                password=otp_record.password,
                role="customer",
                is_active=True,
            )

            otp_record.delete()

    except Exception as error:

        logger.error(
            "Account creation error after OTP verify for %s: %s",
            email,
            error,
            exc_info=True,
        )

        return JsonResponse(
            {
                "success": False,
                "message": "Unable to create account. Please try again.",
            },
            status=500,
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                "Email verified successfully. " "Your account has been created."
            ),
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
            },
        },
        status=200,
    )


# ============================================================
# LOGIN API
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def login_api(request):

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    email = str(data.get("email", "")).strip().lower()

    password = str(data.get("password", ""))

    if not email:

        return JsonResponse(
            {"success": False, "message": "Please enter your email."}, status=400
        )

    if not password:

        return JsonResponse(
            {"success": False, "message": "Please enter your password."}, status=400
        )

    # --------------------------------------------------------
    # FIND USER
    # --------------------------------------------------------

    user = User.objects.filter(email=email, is_active=True).first()

    if user is None:

        return JsonResponse(
            {"success": False, "message": "Invalid email or password."}, status=401
        )

    # --------------------------------------------------------
    # CHECK PASSWORD
    # --------------------------------------------------------

    if not check_password(password, user.password):

        return JsonResponse(
            {"success": False, "message": "Invalid email or password."}, status=401
        )

    # --------------------------------------------------------
    # GET ROLE
    # --------------------------------------------------------

    user_role = str(user.role).strip().lower()

    allowed_roles = ["customer", "staff", "admin"]

    if user_role not in allowed_roles:

        return JsonResponse(
            {"success": False, "message": ("User role is not configured correctly.")},
            status=403,
        )

    # --------------------------------------------------------
    # CREATE SESSION
    # --------------------------------------------------------

    request.session.flush()

    request.session["user_id"] = user.id

    request.session["user_email"] = user.email

    request.session["username"] = user.username

    request.session["user_role"] = user_role

    request.session["is_logged_in"] = True

    request.session.set_expiry(0)

    # --------------------------------------------------------
    # ROLE REDIRECT
    # --------------------------------------------------------

    if user_role == "customer":

        redirect_url = "/customerDashboard.html/"

    elif user_role == "staff":

        redirect_url = "/staffdashboard.html/"

    elif user_role == "admin":

        redirect_url = "/admindashboard.html/"

    else:

        redirect_url = "/login.html/"

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "Login successful.",
            "redirect": redirect_url,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user_role,
            },
        },
        status=200,
    )


# ============================================================
# LOGOUT API
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def logout_api(request):

    request.session.flush()

    return JsonResponse(
        {
            "success": True,
            "message": "Logged out successfully.",
            "redirect": "/login.html/",
        },
        status=200,
    )


# ============================================================
# CURRENT USER API
# ============================================================


@require_http_methods(["GET"])
def current_user_api(request):

    user_id = request.session.get("user_id")

    if not user_id:

        return JsonResponse(
            {
                "success": False,
                "authenticated": False,
                "message": "User is not logged in.",
            },
            status=401,
        )

    try:

        user = User.objects.get(id=user_id, is_active=True)

    except User.DoesNotExist:

        request.session.flush()

        return JsonResponse(
            {
                "success": False,
                "authenticated": False,
                "message": "User session is invalid.",
            },
            status=401,
        )

    return JsonResponse(
        {
            "success": True,
            "authenticated": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
            },
        },
        status=200,
    )


# ============================================================
# LOGIN PAGE
# ============================================================


def login_page(request):

    return render(request, "login.html")


# ============================================================
# CUSTOMER DASHBOARD
# ============================================================


def customer_dashboard(request):

    user_id = request.session.get("user_id")

    role = request.session.get("user_role")

    if not user_id:

        return render(request, "login.html")

    if role != "customer":

        return render(request, "login.html")

    try:

        user = User.objects.get(id=user_id, role="customer", is_active=True)

    except User.DoesNotExist:

        request.session.flush()

        return render(request, "login.html")

    return render(request, "customerDashboard.html", {"user": user})


# ============================================================
# STAFF DASHBOARD
# ============================================================


def staff_dashboard(request):

    user_id = request.session.get("user_id")

    role = request.session.get("user_role")

    if not user_id:

        return render(request, "login.html")

    if role != "staff":

        return render(request, "login.html")

    try:

        user = User.objects.get(id=user_id, role="staff", is_active=True)

    except User.DoesNotExist:

        request.session.flush()

        return render(request, "login.html")

    return render(request, "staffdashboard.html", {"user": user})


# ============================================================
# ADMIN DASHBOARD
# ============================================================


def admin_dashboard(request):

    user_id = request.session.get("user_id")

    role = request.session.get("user_role")

    if not user_id:

        return render(request, "login.html")

    if role != "admin":

        return render(request, "login.html")

    try:

        user = User.objects.get(id=user_id, role="admin", is_active=True)

    except User.DoesNotExist:

        request.session.flush()

        return render(request, "login.html")

    return render(request, "admindashboard.html", {"user": user})


# ============================================================
# JOIN QUEUE API
# ============================================================
# IMPORTANT:
# Customer joins WITHOUT getting a token.
# Staff generates the token later.
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def join_queue_api(request):

    # ========================================================
    # CUSTOMER JOINS QUEUE
    # ========================================================
    # The Django server generates the token immediately.
    # JavaScript does NOT generate the token.
    # Staff does NOT generate the token.
    #
    # Example:
    # Customer 1 -> A001
    # Customer 2 -> A002
    # Customer 3 -> A003
    # ========================================================

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    user_id = request.session.get("user_id")

    if not user_id:
        return JsonResponse(
            {"success": False, "message": "Please login first."}, status=401
        )

    # --------------------------------------------------------
    # GET CUSTOMER
    # --------------------------------------------------------

    try:
        user = User.objects.get(id=user_id, role="customer", is_active=True)
    except User.DoesNotExist:
        request.session.flush()
        return JsonResponse(
            {"success": False, "message": "Invalid customer session."}, status=401
        )

    # --------------------------------------------------------
    # READ REQUEST DATA
    # --------------------------------------------------------

    data = get_json_body(request)

    if data is None:
        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    service = str(data.get("service", "")).strip()

    if not service:
        return JsonResponse(
            {"success": False, "message": "Please select a service."}, status=400
        )

    # --------------------------------------------------------
    # CHECK EXISTING ACTIVE QUEUE
    # --------------------------------------------------------
    # If an old queue exists without a token, generate a token
    # for that queue. This also fixes queues created before the
    # automatic token-generation change.
    # --------------------------------------------------------

    existing_queue = (
        Queue.objects.filter(user=user, status__in=["waiting", "called", "serving"])
        .order_by("-created_at")
        .first()
    )

    if existing_queue:

        # ----------------------------------------------------
        # TOKEN ALREADY EXISTS
        # ----------------------------------------------------

        if existing_queue.token_number is not None:

            if existing_queue.status == "waiting":
                position = Queue.objects.filter(
                    status="waiting", created_at__lte=existing_queue.created_at
                ).count()
            else:
                position = 0

            people_ahead = max(0, position - 1)

            estimated_wait = calculate_customer_wait_time(existing_queue, people_ahead)

            return JsonResponse(
                {
                    "success": True,
                    "message": "You are already in a queue.",
                    "token": existing_queue.token,
                    "token_number": existing_queue.token_number,
                    "position": position,
                    "people_ahead": people_ahead,
                    "estimated_wait": f"{estimated_wait} min",
                    "status": existing_queue.status,
                    "queue": {
                        "id": existing_queue.id,
                        "token": existing_queue.token,
                        "token_number": existing_queue.token_number,
                        "service": existing_queue.service,
                        "position": position,
                        "peopleAhead": people_ahead,
                        "estimatedWait": f"{estimated_wait} min",
                        "status": existing_queue.status,
                    },
                },
                status=200,
            )

        # ----------------------------------------------------
        # OLD QUEUE WITHOUT TOKEN
        # ----------------------------------------------------

        try:
            with transaction.atomic():

                # Lock the queue so two requests cannot update
                # the same old queue at the same time.
                queue = Queue.objects.select_for_update().get(id=existing_queue.id)

                # Check again after locking the row.
                if queue.token_number is None:

                    # Lock the token counter.
                    (
                        counter,
                        created,
                    ) = QueueTokenCounter.objects.select_for_update().get_or_create(
                        name="main", defaults={"last_token_number": 0}
                    )

                    # Generate the next token number.
                    counter.last_token_number += 1

                    counter.save(update_fields=["last_token_number", "updated_at"])

                    # Save the generated number to the queue.
                    queue.token_number = counter.last_token_number

                    queue.save(update_fields=["token_number", "updated_at"])

        except Exception as error:
            print("Existing queue token generation error:", error)

            return JsonResponse(
                {"success": False, "message": "Unable to generate token."}, status=500
            )

        # ----------------------------------------------------
        # POSITION FOR EXISTING QUEUE
        # ----------------------------------------------------

        if queue.status == "waiting":
            position = Queue.objects.filter(
                status="waiting", created_at__lte=queue.created_at
            ).count()
        else:
            position = 0

        people_ahead = max(0, position - 1)

        estimated_wait = calculate_customer_wait_time(queue, people_ahead)

        # ----------------------------------------------------
        # RETURN GENERATED TOKEN
        # ----------------------------------------------------

        return JsonResponse(
            {
                "success": True,
                "message": "Your token has been generated successfully.",
                "token": queue.token,
                "token_number": queue.token_number,
                "position": position,
                "people_ahead": people_ahead,
                "estimated_wait": f"{estimated_wait} min",
                "status": queue.status,
                "queue": {
                    "id": queue.id,
                    "token": queue.token,
                    "token_number": queue.token_number,
                    "service": queue.service,
                    "position": position,
                    "peopleAhead": people_ahead,
                    "estimatedWait": f"{estimated_wait} min",
                    "status": queue.status,
                },
            },
            status=200,
        )

    # --------------------------------------------------------
    # CREATE NEW QUEUE AND GENERATE TOKEN
    # --------------------------------------------------------

    try:
        with transaction.atomic():

            # Lock the counter so simultaneous customers receive
            # different token numbers.
            (
                counter,
                created,
            ) = QueueTokenCounter.objects.select_for_update().get_or_create(
                name="main", defaults={"last_token_number": 0}
            )

            # Generate next token number.
            counter.last_token_number += 1

            counter.save(update_fields=["last_token_number", "updated_at"])

            # Create the queue with the generated token.
            queue = Queue.objects.create(
                user=user,
                service=service,
                token_number=counter.last_token_number,
                status="waiting",
            )

    except Exception as error:
        print("Queue creation/token generation error:", error)

        return JsonResponse(
            {"success": False, "message": "Unable to join queue."}, status=500
        )

    # --------------------------------------------------------
    # POSITION
    # --------------------------------------------------------

    position = Queue.objects.filter(
        status="waiting", created_at__lte=queue.created_at
    ).count()

    people_ahead = max(0, position - 1)

    estimated_wait = calculate_customer_wait_time(queue, people_ahead)

    # --------------------------------------------------------
    # RETURN TOKEN TO CUSTOMER
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "You joined the queue successfully.",
            "token": queue.token,
            "token_number": queue.token_number,
            "position": position,
            "people_ahead": people_ahead,
            "estimated_wait": f"{estimated_wait} min",
            "status": queue.status,
            "queue": {
                "id": queue.id,
                "token": queue.token,
                "token_number": queue.token_number,
                "service": queue.service,
                "position": position,
                "peopleAhead": people_ahead,
                "estimatedWait": f"{estimated_wait} min",
                "status": queue.status,
            },
        },
        status=201,
    )


# ============================================================
# CURRENT CUSTOMER QUEUE API
# ============================================================


@require_http_methods(["GET"])
def my_queue_api(request):

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    user_id = request.session.get("user_id")

    if not user_id:

        return JsonResponse(
            {"success": False, "message": "Please login first."}, status=401
        )

    # --------------------------------------------------------
    # GET ACTIVE QUEUE
    # --------------------------------------------------------

    queue = (
        Queue.objects.filter(
            user_id=user_id, status__in=["waiting", "called", "serving"]
        )
        .order_by("-created_at")
        .first()
    )

    # --------------------------------------------------------
    # NO QUEUE
    # --------------------------------------------------------

    if queue is None:

        return JsonResponse(
            {
                "success": True,
                "has_queue": False,
                "message": "You are not currently " "in a queue.",
            },
            status=200,
        )

    # --------------------------------------------------------
    # POSITION
    # --------------------------------------------------------

    if queue.status == "waiting":

        position = Queue.objects.filter(
            status="waiting", created_at__lte=queue.created_at
        ).count()

    else:

        position = 0

    people_ahead = max(0, position - 1)

    estimated_wait = calculate_customer_wait_time(queue, people_ahead)

    # --------------------------------------------------------
    # TOTAL WAITING
    # --------------------------------------------------------

    total_waiting = Queue.objects.filter(status="waiting").count()

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

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
                "status": queue.status,
                "total": total_waiting,
            },
        },
        status=200,
    )


# ============================================================
# LEAVE QUEUE API
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def leave_queue_api(request):

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    user_id = request.session.get("user_id")

    if not user_id:

        return JsonResponse(
            {"success": False, "message": "Please login first."}, status=401
        )

    # --------------------------------------------------------
    # FIND ACTIVE QUEUE
    # --------------------------------------------------------

    queue = (
        Queue.objects.filter(
            user_id=user_id, status__in=["waiting", "called", "serving"]
        )
        .order_by("-created_at")
        .first()
    )

    if queue is None:

        return JsonResponse(
            {"success": False, "message": "You are not currently " "in a queue."},
            status=404,
        )

    # --------------------------------------------------------
    # CANCEL QUEUE
    # --------------------------------------------------------

    queue.status = "cancelled"

    queue.save(update_fields=["status", "updated_at"])

    return JsonResponse(
        {"success": True, "message": "You have left the queue.", "token": queue.token},
        status=200,
    )


# ============================================================
# QUEUE STATUS PAGE
# ============================================================


@require_http_methods(["GET"])
def queue_status_page(request):

    user_id = request.session.get("user_id")

    role = request.session.get("user_role")

    # --------------------------------------------------------
    # LOGIN CHECK
    # --------------------------------------------------------

    if not user_id:

        return render(request, "login.html")

    # --------------------------------------------------------
    # CUSTOMER ONLY
    # --------------------------------------------------------

    if role != "customer":

        return render(request, "login.html")

    # --------------------------------------------------------
    # VERIFY USER
    # --------------------------------------------------------

    try:

        user = User.objects.get(id=user_id, role="customer", is_active=True)

    except User.DoesNotExist:

        request.session.flush()

        return render(request, "login.html")

    # --------------------------------------------------------
    # RENDER QUEUE STATUS
    # --------------------------------------------------------

    return render(request, "queuestatus.html", {"user": user})


# ============================================================
# STAFF - GET QUEUE
# ============================================================


@require_http_methods(["GET"])
def staff_queue_api(request):

    # --------------------------------------------------------
    # CHECK STAFF LOGIN
    # --------------------------------------------------------

    staff = get_staff_user(request)

    if not staff:

        return JsonResponse(
            {"success": False, "message": "Staff login required."}, status=403
        )

    # --------------------------------------------------------
    # GET ACTIVE QUEUES
    # --------------------------------------------------------

    queues = (
        Queue.objects.filter(status__in=["waiting", "called", "serving"])
        .select_related("user")
        .order_by("created_at")
    )

    queue_list = []

    for queue in queues:

        queue_list.append(queue_to_dict(queue))

    # --------------------------------------------------------
    # COUNTS
    # --------------------------------------------------------

    waiting_count = Queue.objects.filter(status="waiting").count()

    completed_count = Queue.objects.filter(status="completed").count()

    # --------------------------------------------------------
    # CURRENT CUSTOMER
    # --------------------------------------------------------

    current_serving = (
        Queue.objects.filter(status__in=["called", "serving"])
        .select_related("user")
        .order_by("created_at")
        .first()
    )

    current_serving_data = None

    if current_serving:

        current_serving_data = queue_to_dict(current_serving)

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "queues": queue_list,
            "waiting_count": waiting_count,
            "completed_count": completed_count,
            "current_serving": current_serving_data,
        },
        status=200,
    )


# ============================================================
# STAFF - GENERATE TOKEN
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def generate_token_api(request):

    # --------------------------------------------------------
    # CHECK STAFF
    # --------------------------------------------------------

    staff = get_staff_user(request)

    if not staff:

        return JsonResponse(
            {"success": False, "message": "Staff login required."}, status=403
        )

    # --------------------------------------------------------
    # READ DATA
    # --------------------------------------------------------

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    queue_id = data.get("queue_id")

    if not queue_id:

        return JsonResponse(
            {"success": False, "message": "Queue ID is required."}, status=400
        )

    # --------------------------------------------------------
    # GET QUEUE
    # --------------------------------------------------------

    try:

        queue = Queue.objects.get(id=queue_id, status="waiting")

    except Queue.DoesNotExist:

        return JsonResponse(
            {"success": False, "message": "Waiting customer not found."}, status=404
        )

    # --------------------------------------------------------
    # CHECK IF TOKEN ALREADY EXISTS
    # --------------------------------------------------------

    if queue.token_number is not None:

        return JsonResponse(
            {
                "success": False,
                "message": "This customer already has a token.",
                "queue": queue_to_dict(queue),
            },
            status=400,
        )

    # --------------------------------------------------------
    # GENERATE TOKEN
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            (
                counter,
                created,
            ) = QueueTokenCounter.objects.select_for_update().get_or_create(
                name="main", defaults={"last_token_number": 0}
            )

            counter.last_token_number += 1

            counter.save()

            queue.token_number = counter.last_token_number

            queue.save()

    except Exception as error:

        print("Token generation error:", error)

        return JsonResponse(
            {"success": False, "message": "Unable to generate token."}, status=500
        )

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "Token generated successfully.",
            "queue": queue_to_dict(queue),
        },
        status=200,
    )


# ============================================================
# STAFF - CALL NEXT CUSTOMER
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def call_next_api(request):

    # --------------------------------------------------------
    # CHECK STAFF
    # --------------------------------------------------------

    staff = get_staff_user(request)

    if not staff:

        return JsonResponse(
            {"success": False, "message": "Staff login required."}, status=403
        )

    # --------------------------------------------------------
    # CHECK CURRENT SERVING CUSTOMER
    # --------------------------------------------------------

    current = (
        Queue.objects.filter(status__in=["called", "serving"])
        .select_related("user")
        .order_by("created_at")
        .first()
    )

    if current:

        return JsonResponse(
            {
                "success": False,
                "message": "A customer is already being served.",
                "queue": queue_to_dict(current),
            },
            status=400,
        )

    # --------------------------------------------------------
    # FIND FIRST WAITING CUSTOMER
    # --------------------------------------------------------

    next_queue = (
        Queue.objects.filter(status="waiting")
        .select_related("user")
        .order_by("created_at")
        .first()
    )

    if not next_queue:

        return JsonResponse(
            {"success": False, "message": "No waiting customer."}, status=404
        )

    # --------------------------------------------------------
    # GENERATE TOKEN IF NOT GENERATED
    # --------------------------------------------------------

    try:

        with transaction.atomic():

            if next_queue.token_number is None:

                (
                    counter,
                    created,
                ) = QueueTokenCounter.objects.select_for_update().get_or_create(
                    name="main", defaults={"last_token_number": 0}
                )

                counter.last_token_number += 1

                counter.save()

                next_queue.token_number = counter.last_token_number

            # ------------------------------------------------
            # CALL CUSTOMER
            # ------------------------------------------------

            next_queue.status = "called"

            next_queue.save()

    except Exception as error:

        print("Call next error:", error)

        return JsonResponse(
            {"success": False, "message": "Unable to call next customer."}, status=500
        )

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "Next customer called successfully.",
            "queue": queue_to_dict(next_queue),
        },
        status=200,
    )


# ============================================================
# STAFF - COMPLETE TOKEN
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def complete_token_api(request):

    # --------------------------------------------------------
    # CHECK STAFF
    # --------------------------------------------------------

    staff = get_staff_user(request)

    if not staff:

        return JsonResponse(
            {"success": False, "message": "Staff login required."}, status=403
        )

    # --------------------------------------------------------
    # READ DATA
    # --------------------------------------------------------

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    queue_id = data.get("queue_id")

    if not queue_id:

        return JsonResponse(
            {"success": False, "message": "Queue ID is required."}, status=400
        )

    # --------------------------------------------------------
    # FIND QUEUE
    # --------------------------------------------------------

    try:

        queue = Queue.objects.get(id=queue_id)

    except Queue.DoesNotExist:

        return JsonResponse(
            {"success": False, "message": "Queue not found."}, status=404
        )

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    queue.status = "completed"

    queue.save()

    return JsonResponse(
        {
            "success": True,
            "message": "Customer service completed.",
            "queue": queue_to_dict(queue),
        },
        status=200,
    )


# ============================================================
# STAFF - SKIP TOKEN
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def skip_token_api(request):

    # --------------------------------------------------------
    # CHECK STAFF
    # --------------------------------------------------------

    staff = get_staff_user(request)

    if not staff:

        return JsonResponse(
            {"success": False, "message": "Staff login required."}, status=403
        )

    # --------------------------------------------------------
    # READ DATA
    # --------------------------------------------------------

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    queue_id = data.get("queue_id")

    if not queue_id:

        return JsonResponse(
            {"success": False, "message": "Queue ID is required."}, status=400
        )

    # --------------------------------------------------------
    # FIND QUEUE
    # --------------------------------------------------------

    try:

        queue = Queue.objects.get(id=queue_id)

    except Queue.DoesNotExist:

        return JsonResponse(
            {"success": False, "message": "Queue not found."}, status=404
        )

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    queue.status = "cancelled"

    queue.save()

    return JsonResponse(
        {
            "success": True,
            "message": "Customer skipped successfully.",
            "queue": queue_to_dict(queue),
        },
        status=200,
    )


# ============================================================
# ADMIN - GET ALL CUSTOMERS
# ============================================================


@require_http_methods(["GET"])
def admin_customers_api(request):

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    if not request.session.get("is_logged_in"):

        return JsonResponse(
            {"success": False, "message": "Login required."}, status=401
        )

    # --------------------------------------------------------
    # CHECK ADMIN
    # --------------------------------------------------------

    if request.session.get("user_role") != "admin":

        return JsonResponse(
            {"success": False, "message": "Admin access required."}, status=403
        )

    # --------------------------------------------------------
    # GET CUSTOMERS
    # --------------------------------------------------------

    customers = User.objects.filter(role="customer", is_active=True).order_by(
        "-created_at"
    )

    customer_list = []

    for customer in customers:

        customer_list.append(
            {
                "id": customer.id,
                "username": customer.username,
                "email": customer.email,
                "role": customer.role,
                "is_active": customer.is_active,
                "created_at": customer.created_at.strftime("%Y-%m-%d %H:%M"),
            }
        )

    return JsonResponse(
        {"success": True, "customers": customer_list, "count": len(customer_list)},
        status=200,
    )


# ============================================================
# ADMIN - GET ALL STAFF
# ============================================================


@require_http_methods(["GET"])
def admin_staff_api(request):

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    if not request.session.get("is_logged_in"):

        return JsonResponse(
            {"success": False, "message": "Login required."}, status=401
        )

    # --------------------------------------------------------
    # CHECK ADMIN
    # --------------------------------------------------------

    if request.session.get("user_role") != "admin":

        return JsonResponse(
            {"success": False, "message": "Admin access required."}, status=403
        )

    # --------------------------------------------------------
    # GET STAFF
    # --------------------------------------------------------

    staff_users = User.objects.filter(role="staff", is_active=True).order_by(
        "-created_at"
    )

    staff_list = []

    for staff in staff_users:

        staff_list.append(
            {
                "id": staff.id,
                "username": staff.username,
                "email": staff.email,
                "role": staff.role,
                "is_active": staff.is_active,
                "created_at": staff.created_at.strftime("%Y-%m-%d %H:%M"),
            }
        )

    return JsonResponse(
        {"success": True, "staff": staff_list, "count": len(staff_list)}, status=200
    )


# ============================================================
# ADMIN - CREATE STAFF ACCOUNT
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def create_staff_api(request):

    # --------------------------------------------------------
    # CHECK LOGIN
    # --------------------------------------------------------

    if not request.session.get("is_logged_in"):

        return JsonResponse(
            {"success": False, "message": "Login required."}, status=401
        )

    # --------------------------------------------------------
    # CHECK ADMIN
    # --------------------------------------------------------

    if request.session.get("user_role") != "admin":

        return JsonResponse(
            {"success": False, "message": "Only admin can create " "staff accounts."},
            status=403,
        )

    # --------------------------------------------------------
    # READ JSON
    # --------------------------------------------------------

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid request data."}, status=400
        )

    # --------------------------------------------------------
    # GET DATA
    # --------------------------------------------------------

    name = str(data.get("username", "")).strip()

    email = str(data.get("email", "")).strip().lower()

    password = str(data.get("password", ""))

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if not name:

        return JsonResponse(
            {"success": False, "message": "Staff name is required."}, status=400
        )

    if not email:

        return JsonResponse(
            {"success": False, "message": "Email is required."}, status=400
        )

    if not password:

        return JsonResponse(
            {"success": False, "message": "Password is required."}, status=400
        )

    if len(password) < 6:

        return JsonResponse(
            {
                "success": False,
                "message": "Password must contain " "at least 6 characters.",
            },
            status=400,
        )

    # --------------------------------------------------------
    # CHECK EMAIL
    # --------------------------------------------------------
    # Keep email unique across the whole system.
    # This also makes login role selection unambiguous.
    # --------------------------------------------------------

    existing_user = User.objects.filter(email=email).first()

    if existing_user:

        return JsonResponse(
            {"success": False, "message": "This email is already registered."},
            status=400,
        )

    # --------------------------------------------------------
    # CREATE STAFF
    # --------------------------------------------------------

    try:

        staff = User.objects.create(
            username=name,
            email=email,
            password=make_password(password),
            role="staff",
            is_active=True,
        )

    except Exception as error:

        print("Staff creation error:", error)

        return JsonResponse(
            {"success": False, "message": "Unable to create staff account."}, status=500
        )

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

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


# ============================================================
# RESEND OTP API
# ============================================================


@csrf_exempt
@require_http_methods(["POST"])
def resend_otp(request):

    data = get_json_body(request)

    if data is None:

        return JsonResponse(
            {"success": False, "message": "Invalid JSON data."}, status=400
        )

    email = str(data.get("email", "")).strip().lower()

    if not email:

        return JsonResponse(
            {"success": False, "message": "Email is required."}, status=400
        )

    # --------------------------------------------------------
    # FIND TEMPORARY SIGNUP
    # --------------------------------------------------------

    otp_record = EmailOTP.objects.filter(email=email).order_by("-created_at").first()

    if not otp_record:

        return JsonResponse(
            {
                "success": False,
                "message": "Signup session not found. Please register again.",
            },
            status=404,
        )

    # --------------------------------------------------------
    # GENERATE NEW OTP
    # --------------------------------------------------------

    new_otp = str(random.randint(100000, 999999))

    otp_record.otp = new_otp

    otp_record.created_at = timezone.now()

    otp_record.save(update_fields=["otp", "created_at"])

    # --------------------------------------------------------
    # SEND NEW OTP
    # --------------------------------------------------------

    try:

        send_otp_email(
            otp_record.username,
            email,
            new_otp,
            is_resend=True,
        )

    except Exception as error:

        logger.error(
            "Resend OTP error for %s: %s",
            email,
            error,
            exc_info=True,
        )

        return JsonResponse(
            {"success": False, "message": describe_email_error(error)}, status=500
        )

    return JsonResponse(
        {"success": True, "message": "A new OTP has been sent to your email."},
        status=200,
    )
