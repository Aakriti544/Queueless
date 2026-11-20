import json
import logging
import random
import secrets
from datetime import timedelta
from html import escape

from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from user.models import User, EmailOTP, PasswordResetOTP
from user.services.brevo_email import BrevoEmailError, send_brevo_email

logger = logging.getLogger(__name__)

PASSWORD_RESET_GENERIC = "If an account exists for that email, a verification code has been sent."


def _reset_flow(request):
    flow_id = request.session.get("password_reset_flow")
    if not flow_id:
        return None
    return PasswordResetOTP.objects.filter(flow_id=flow_id).first()


def _mask_email(email):
    local, separator, domain = email.partition("@")
    if not separator:
        return email
    return (local[:1] + "***" if local else "***") + "@" + domain


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_request_api(request):
    data = get_json_body(request)
    if data is None:
        return JsonResponse({"success": False, "message": "Invalid request."}, status=400)
    email = str(data.get("email", "")).strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return JsonResponse({"success": False, "message": "Enter a valid email address."}, status=400)

    now = timezone.now()
    # Per-address cooldown and rolling request limit apply even to unknown addresses.
    recent = PasswordResetOTP.objects.filter(email__iexact=email, created_at__gte=now - timedelta(minutes=15))
    if recent.filter(created_at__gte=now - timedelta(seconds=60)).exists():
        request.session["password_reset_flow"] = recent.order_by("-created_at").first().flow_id
        return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC, "next": "/verify_otp.html/"})
    if recent.count() >= 4:
        return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC, "next": "/verify_otp.html/"})

    flow_id = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = PasswordResetOTP.objects.create(
        email=email,
        flow_id=flow_id,
        otp_hash=make_password(code),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    request.session["password_reset_flow"] = flow_id
    request.session["password_reset_email"] = email
    user = User.objects.filter(email__iexact=email, is_active=True).order_by("-created_at").first()
    if user:
        try:
            send_brevo_email(
                recipient=user.email,
                subject="QueueLess password reset code",
                text_content=f"Hello {user.username},\n\nYour QueueLess password reset code is {code}. It expires in 5 minutes. Do not share it with anyone.",
                html_content=f"<p>Hello {escape(user.username)},</p><p>Your QueueLess password reset code is <strong>{code}</strong>.</p><p>It expires in 5 minutes. Do not share it with anyone.</p>",
            )
        except Exception as error:
            logger.warning("Password reset email delivery failed (%s).", type(error).__name__)
            record.delete()
            return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC, "next": "/verify_otp.html/"})
    return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC, "next": "/verify_otp.html/"})


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_resend_api(request):
    record = _reset_flow(request)
    now = timezone.now()
    if not record or record.consumed_at or record.verified_at:
        return JsonResponse({"success": False, "message": "Please start a new password reset request."}, status=400)
    if (now - record.created_at).total_seconds() < 60:
        return JsonResponse({"success": False, "message": "Please wait before requesting another code."}, status=429)
    if record.resend_count >= 3:
        return JsonResponse({"success": False, "message": "Resend limit reached. Please start again later."}, status=429)
    recent = PasswordResetOTP.objects.filter(email__iexact=record.email, created_at__gte=now - timedelta(minutes=15)).count()
    if recent >= 4:
        return JsonResponse({"success": False, "message": "Request limit reached. Please try again later."}, status=429)
    code = f"{secrets.randbelow(1_000_000):06d}"
    user = User.objects.filter(email__iexact=record.email, is_active=True).order_by("-created_at").first()
    if user:
        try:
            send_brevo_email(recipient=user.email, subject="QueueLess password reset code", text_content=f"Your new QueueLess password reset code is {code}. It expires in 5 minutes. Do not share it.")
        except Exception as error:
            logger.warning("Password reset resend failed (%s).", type(error).__name__)
            return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC})
    record.otp_hash = make_password(code)
    record.created_at = now
    record.expires_at = now + timedelta(minutes=5)
    record.attempts = 0
    record.resend_count += 1
    record.save(update_fields=["otp_hash", "created_at", "expires_at", "attempts", "resend_count"])
    return JsonResponse({"success": True, "message": PASSWORD_RESET_GENERIC})


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_verify_api(request):
    data = get_json_body(request)
    record = _reset_flow(request)
    if data is None or not record or record.consumed_at or record.verified_at:
        return JsonResponse({"success": False, "message": "This reset request is no longer valid. Start again."}, status=400)
    now = timezone.now()
    if record.expires_at <= now:
        return JsonResponse({"success": False, "message": "That code expired. Request a new one."}, status=400)
    code = str(data.get("otp", "")).strip()
    if record.attempts >= 5:
        record.consumed_at = now
        record.save(update_fields=["consumed_at"])
        return JsonResponse({"success": False, "message": "Too many attempts. Start a new request."}, status=429)
    if len(code) != 6 or not code.isdigit() or not check_password(code, record.otp_hash):
        record.attempts += 1
        if record.attempts >= 5:
            record.consumed_at = now
        record.save(update_fields=["attempts", "consumed_at"] if record.attempts >= 5 else ["attempts"])
        return JsonResponse({"success": False, "message": "Incorrect or expired code."}, status=400)
    record.verified_at = now
    record.save(update_fields=["verified_at"])
    request.session["password_reset_verified"] = record.flow_id
    return JsonResponse({"success": True, "next": "/reset_password.html/"})


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_complete_api(request):
    data = get_json_body(request)
    record = _reset_flow(request)
    if data is None or not record or record.consumed_at or not record.verified_at or request.session.get("password_reset_verified") != record.flow_id:
        return JsonResponse({"success": False, "message": "Verify your email code before resetting the password."}, status=403)
    if record.expires_at <= timezone.now():
        return JsonResponse({"success": False, "message": "Your verification expired. Start a new request."}, status=400)
    password = str(data.get("password", ""))
    confirm = str(data.get("confirm_password", ""))
    if password != confirm:
        return JsonResponse({"success": False, "message": "Passwords do not match."}, status=400)
    if len(password) < 8 or len(password) > 128:
        return JsonResponse({"success": False, "message": "Password must be between 8 and 128 characters."}, status=400)
    user = User.objects.filter(email__iexact=record.email, is_active=True).order_by("-created_at").first()
    if not user:
        record.consumed_at = timezone.now()
        record.save(update_fields=["consumed_at"])
        return JsonResponse({"success": False, "message": "The reset request is no longer valid. Start again."}, status=400)
    with transaction.atomic():
        user.password = make_password(password)
        user.save(update_fields=["password", "updated_at"])
        record.consumed_at = timezone.now()
        record.save(update_fields=["consumed_at"])
    request.session.flush()
    request.session["user_id"] = user.id
    request.session["user_email"] = user.email
    request.session["username"] = user.username
    request.session["user_role"] = user.role
    request.session["is_logged_in"] = True
    request.session.set_expiry(0)
    redirect = {"customer": "/customerDashboard.html/", "staff": "/staffdashboard.html/", "admin": "/admindashboard.html/"}.get(user.role, "/login.html/")
    return JsonResponse({"success": True, "message": "Your password has been reset successfully!", "redirect": redirect})


def forgot_password_page(request):
    from django.shortcuts import render
    return render(request, "forgot_password.html")


def verify_password_otp_page(request):
    from django.shortcuts import render
    email = request.session.get("password_reset_email", "")
    return render(request, "verify_otp.html", {"masked_email": _mask_email(email) if email else "your email"})


def reset_password_page(request):
    from django.shortcuts import render
    if not request.session.get("password_reset_verified"):
        from django.shortcuts import redirect
        return redirect("/forgot_password.html/")
    return render(request, "reset_password.html")


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
    safe_username = escape(username)
    safe_otp = escape(otp)
    html_message = (
        f"<p>Hello {safe_username},</p>"
        f"<p>{escape(headline)}: <strong>{safe_otp}</strong></p>"
        "<p>This OTP is valid for 5 minutes.</p>"
        "<p>Do not share this code with anyone.</p>"
        "<p>Thank you,<br>QueueLess Team</p>"
    )

    send_brevo_email(
        recipient=email,
        subject=subject,
        text_content=message,
        html_content=html_message,
    )


def describe_email_error(error):
    if isinstance(error, BrevoEmailError):
        return str(error)
    return "We couldn't send your verification email. Please try again shortly."


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

        logger.warning("Signup OTP delivery failed (%s).", type(error).__name__)

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

    matching_users = [
        account
        for account in User.objects.filter(email=email, is_active=True)
        if check_password(password, account.password)
    ]

    if not matching_users:

        return JsonResponse(
            {"success": False, "message": "Invalid email or password."}, status=401
        )

    # The schema permits the same email for different roles. Never guess
    # which account the user intended when credentials match more than one.
    if len(matching_users) > 1:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "These credentials match more than one account. "
                    "Please contact an administrator."
                ),
            },
            status=409,
        )

    user = matching_users[0]

    # --------------------------------------------------------
    # CHECK PASSWORD
    # --------------------------------------------------------

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

        logger.warning("Resend OTP delivery failed (%s).", type(error).__name__)

        return JsonResponse(
            {"success": False, "message": describe_email_error(error)}, status=500
        )

    return JsonResponse(
        {"success": True, "message": "A new OTP has been sent to your email."},
        status=200,
    )
