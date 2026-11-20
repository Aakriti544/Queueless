"""Helpers for validating and encrypting browser Web Push subscriptions."""

import base64
import hashlib
import json
import os
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
    return push_configuration_status()["configured"]


def push_configuration_status():
    """Return safe, non-secret diagnostics for Web Push configuration."""
    public_present = bool(settings.WEB_PUSH_VAPID_PUBLIC_KEY)
    private_present = bool(settings.WEB_PUSH_VAPID_PRIVATE_KEY)
    subject = settings.WEB_PUSH_VAPID_SUBJECT
    subject_present = bool(subject)
    encryption_present = bool(settings.WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY)

    try:
        public_bytes = application_server_key_bytes()
        public_valid = len(public_bytes) == 65 and public_bytes[0] == 4
    except (ImproperlyConfigured, ValueError, TypeError):
        public_valid = False

    private_valid = False
    keys_match = False
    if private_present:
        try:
            from py_vapid import Vapid
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

            private_value = settings.WEB_PUSH_VAPID_PRIVATE_KEY
            vapid = (
                Vapid.from_file(private_key_file=private_value)
                if os.path.isfile(private_value)
                else Vapid.from_string(private_value)
            )
            derived_public = vapid.public_key.public_bytes(
                Encoding.X962, PublicFormat.UncompressedPoint
            )
            private_valid = len(derived_public) == 65 and derived_public[0] == 4
            keys_match = public_valid and private_valid and derived_public == public_bytes
        except Exception:
            # Keep diagnostics safe: never include key material or parser errors.
            private_valid = False
            keys_match = False

    subject_valid = False
    if subject_present:
        if subject.startswith("mailto:"):
            subject_valid = bool(subject[7:].strip())
        else:
            try:
                parsed_subject = urlsplit(subject)
                subject_valid = parsed_subject.scheme == "https" and bool(parsed_subject.netloc)
            except ValueError:
                subject_valid = False

    try:
        _fernet()
        encryption_valid = True
    except ImproperlyConfigured:
        encryption_valid = False

    checks = {
        "WEB_PUSH_VAPID_PUBLIC_KEY": public_present and public_valid,
        "WEB_PUSH_VAPID_PRIVATE_KEY": private_present and private_valid,
        "WEB_PUSH_VAPID_SUBJECT": subject_present and subject_valid,
        "WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY": encryption_present and encryption_valid,
    }
    if public_present and private_present:
        checks["VAPID key pair matches"] = keys_match

    return {
        "configured": all(checks.values()),
        "checks": checks,
        "missing_or_invalid": [name for name, passed in checks.items() if not passed],
    }


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
