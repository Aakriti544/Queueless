"""Helpers for validating and encrypting browser Web Push subscriptions."""

import base64
import hashlib
import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError


_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PUSH_HOST_SUFFIXES = (
    ".push.services.mozilla.com",
    ".notify.windows.com",
    ".push.apple.com",
)
_PUSH_HOSTS = {
    "push.services.mozilla.com",
    "updates.push.services.mozilla.com",
    "fcm.googleapis.com",
    "web.push.apple.com",
}


def push_is_configured():
    configured = all(
        (
            settings.WEB_PUSH_VAPID_PUBLIC_KEY,
            settings.WEB_PUSH_VAPID_PRIVATE_KEY,
            settings.WEB_PUSH_VAPID_SUBJECT,
            settings.WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY,
        )
    )
    if not configured:
        return False
    try:
        return len(application_server_key_bytes()) == 65 and _fernet() is not None
    except ImproperlyConfigured:
        return False


def validate_subscription(value):
    if not isinstance(value, dict):
        raise ValidationError("A valid browser push subscription is required.")
    endpoint = value.get("endpoint")
    keys = value.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValidationError("The push endpoint is invalid.")
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise ValidationError("The push endpoint is invalid.") from error
    allowed_host = hostname in _PUSH_HOSTS or any(
        hostname.endswith(suffix) for suffix in _PUSH_HOST_SUFFIXES
    )
    if (
        parsed.scheme != "https"
        or not allowed_host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not parsed.path
    ):
        raise ValidationError("The push endpoint is not an approved push service.")
    if not isinstance(keys, dict):
        raise ValidationError("Push encryption keys are missing.")
    clean_keys = {}
    for name in ("p256dh", "auth"):
        key = keys.get(name)
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 200
            or not _B64URL_RE.fullmatch(key)
        ):
            raise ValidationError("Push encryption keys are invalid.")
        clean_keys[name] = key
    return {"endpoint": endpoint, "keys": clean_keys}


def endpoint_fingerprint(endpoint):
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _fernet():
    key = settings.WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY
    if not key:
        raise ImproperlyConfigured("Web Push subscription encryption is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise ImproperlyConfigured(
            "WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY must be a Fernet key."
        ) from error


def encrypt_subscription(subscription):
    raw = json.dumps(subscription, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_subscription(encrypted_value):
    try:
        value = json.loads(_fernet().decrypt(encrypted_value.encode("ascii")))
    except (InvalidToken, UnicodeEncodeError, json.JSONDecodeError) as error:
        raise ImproperlyConfigured("Stored Web Push subscription could not be decrypted.") from error
    return validate_subscription(value)


def application_server_key_bytes():
    key = settings.WEB_PUSH_VAPID_PUBLIC_KEY.strip()
    try:
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except (ValueError, TypeError) as error:
        raise ImproperlyConfigured("WEB_PUSH_VAPID_PUBLIC_KEY is invalid.") from error
