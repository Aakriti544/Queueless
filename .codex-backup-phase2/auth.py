import json
import logging
import random
import smtplib

from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from user.models import User, EmailOTP

logger = logging.getLogger(__name__)


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
