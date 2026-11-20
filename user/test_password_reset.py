import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from django.utils import timezone

from user.models import PasswordResetOTP, User


class PasswordResetWorkflowTests(TestCase):
    def setUp(self):
        self.account = User.objects.create(
            username="Staff Member",
            email="staff@example.com",
            password=make_password("old-pass-123"),
            role="staff",
            is_active=True,
        )
        self.delivery = patch("user.views.auth.send_brevo_email")
        self.send_email = self.delivery.start()
        self.addCleanup(self.delivery.stop)

    def otp_from_email(self):
        message = self.send_email.call_args.kwargs["text_content"]
        return re.search(r"\b(\d{6})\b", message).group(1)

    def begin_reset(self):
        response = self.client.post(
            "/api/user/password-reset/request/",
            data='{"email":"staff@example.com"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next"], "/verify_otp.html/")

    def test_complete_flow_logs_in_and_replaces_password(self):
        self.begin_reset()
        code = self.otp_from_email()
        response = self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"000000"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"%s"}' % code,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/user/password-reset/complete/",
            data='{"password":"new-pass-456","confirm_password":"new-pass-456"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Your password has been reset successfully!")
        self.assertEqual(response.json()["redirect"], "/staffdashboard.html/")
        self.account.refresh_from_db()
        self.assertTrue(check_password("new-pass-456", self.account.password))
        self.assertFalse(check_password("old-pass-123", self.account.password))
        self.assertIsNotNone(PasswordResetOTP.objects.get().consumed_at)
        replay = self.client.post(
            "/api/user/password-reset/complete/",
            data='{"password":"third-pass-789","confirm_password":"third-pass-789"}',
            content_type="application/json",
        )
        self.assertEqual(replay.status_code, 403)

    def test_expired_code_cannot_be_verified(self):
        self.begin_reset()
        challenge = PasswordResetOTP.objects.get()
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
        response = self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"%s"}' % self.otp_from_email(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_mismatch_does_not_change_password(self):
        self.begin_reset()
        self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"%s"}' % self.otp_from_email(),
            content_type="application/json",
        )
        response = self.client.post(
            "/api/user/password-reset/complete/",
            data='{"password":"new-pass-456","confirm_password":"different-456"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.account.refresh_from_db()
        self.assertTrue(check_password("old-pass-123", self.account.password))

    def test_resend_rotates_code_and_obeys_cooldown(self):
        self.begin_reset()
        old_code = self.otp_from_email()
        blocked = self.client.post("/api/user/password-reset/resend/", data="{}", content_type="application/json")
        self.assertEqual(blocked.status_code, 429)
        challenge = PasswordResetOTP.objects.get()
        challenge.created_at = timezone.now() - timedelta(seconds=61)
        challenge.save(update_fields=["created_at"])
        resent = self.client.post("/api/user/password-reset/resend/", data="{}", content_type="application/json")
        self.assertEqual(resent.status_code, 200)
        new_code = self.otp_from_email()
        self.assertNotEqual(old_code, new_code)
        old = self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"%s"}' % old_code,
            content_type="application/json",
        )
        self.assertEqual(old.status_code, 400)
        current = self.client.post(
            "/api/user/password-reset/verify/",
            data='{"otp":"%s"}' % new_code,
            content_type="application/json",
        )
        self.assertEqual(current.status_code, 200)

    def test_unknown_email_gets_same_public_response_without_email(self):
        response = self.client.post(
            "/api/user/password-reset/request/",
            data='{"email":"nobody@example.com"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "If an account exists for that email, a verification code has been sent.")
        self.send_email.assert_not_called()
