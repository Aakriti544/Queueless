import json
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from user.models import (
    CustomerNotification,
    Queue,
    QueueEmailDelivery,
    QueueTokenCounter,
    Service,
    User,
)
from user.services.notifications import (
    MAX_EMAIL_ATTEMPTS,
    enqueue_queue_email,
    process_pending_queue_emails,
)
from user.services.brevo_email import BrevoEmailError


class QueueEmailNotificationTests(TestCase):
    def setUp(self):
        QueueTokenCounter.objects.get_or_create(name="main")
        self.service = Service.objects.create(name="Email Test Service")
        self.customer = self.make_user("email-customer", "customer")
        self.staff = self.make_user("email-staff", "staff")
        self.customer_client = self.client_for(self.customer)
        self.staff_client = self.client_for(self.staff)

    @staticmethod
    def make_user(username, role):
        return User.objects.create(
            username=username,
            email=f"{username}@example.test",
            password="test-hash",
            role=role,
        )

    @staticmethod
    def client_for(user):
        client = Client()
        session = client.session
        session.update(
            {"user_id": user.pk, "user_role": user.role, "is_logged_in": True}
        )
        session.save()
        return client

    def join(self, client):
        return client.post(
            "/api/user/join-queue/",
            data=json.dumps({"service_id": self.service.pk}),
            content_type="application/json",
        )

    def call_next(self):
        return self.staff_client.post(
            "/api/user/call-next/", data="{}", content_type="application/json"
        )

    def test_all_queue_events_enqueue_email_and_keep_in_app_notifications(self):
        # Queue events only enqueue email; delivery uses the Brevo worker.
        with patch("user.services.notifications.send_brevo_email") as send_brevo_email:
            joined = self.join(self.customer_client)
            self.assertEqual(joined.status_code, 201, joined.content)
            queue_id = joined.json()["queue"]["id"]

            # A retry of the join request is the same queue event.
            duplicate_join = self.join(self.customer_client)
            self.assertEqual(duplicate_join.status_code, 200)
            self.assertEqual(QueueEmailDelivery.objects.count(), 1)
            self.customer_client.session.flush()

            called = self.call_next()
            self.assertEqual(called.status_code, 200, called.content)
            started = self.staff_client.post(
                "/api/user/start-service/",
                data=json.dumps({"queue_id": queue_id, "service_duration_minutes": 12}),
                content_type="application/json",
            )
            self.assertEqual(started.status_code, 200, started.content)
            completed = self.staff_client.post(
                "/api/user/complete-token/",
                data=json.dumps({"queue_id": queue_id}),
                content_type="application/json",
            )
            self.assertEqual(completed.status_code, 200, completed.content)

            cancelled_customer = self.make_user("email-cancelled", "customer")
            cancelled_client = self.client_for(cancelled_customer)
            cancelled_join = self.join(cancelled_client)
            self.assertEqual(cancelled_join.status_code, 201)
            cancelled = cancelled_client.post("/api/user/leave-queue/")
            self.assertEqual(cancelled.status_code, 200, cancelled.content)

            skipped_customer = self.make_user("email-skipped", "customer")
            skipped_client = self.client_for(skipped_customer)
            skipped_join = self.join(skipped_client)
            self.assertEqual(skipped_join.status_code, 201)
            skipped_call = self.call_next()
            self.assertEqual(skipped_call.status_code, 200, skipped_call.content)
            skipped_queue_id = skipped_join.json()["queue"]["id"]
            Queue.objects.filter(pk=skipped_queue_id).update(
                grace_deadline=timezone.now() - timedelta(seconds=1)
            )
            skipped = self.staff_client.post(
                "/api/user/skip-token/",
                data=json.dumps({"queue_id": skipped_queue_id}),
                content_type="application/json",
            )
            self.assertEqual(skipped.status_code, 200, skipped.content)

        send_brevo_email.assert_not_called()
        deliveries = list(QueueEmailDelivery.objects.order_by("event_key"))
        self.assertEqual(
            sorted(delivery.event for delivery in deliveries),
            ["called", "called", "cancelled", "completed", "joined", "joined", "joined", "skipped", "started"],
        )
        self.assertEqual(len({delivery.event_key for delivery in deliveries}), 9)
        self.assertTrue(
            all(delivery.status == QueueEmailDelivery.STATUS_PENDING for delivery in deliveries)
        )
        self.assertEqual(CustomerNotification.objects.count(), 9)
        self.assertTrue(
            all(delivery.recipient_email.endswith("@example.test") for delivery in deliveries)
        )

    def test_outbox_email_success_updates_delivery_status(self):
        self.join(self.customer_client)
        delivery = QueueEmailDelivery.objects.get(event="joined")
        self.customer_client.session.flush()

        with patch("user.services.notifications.send_brevo_email") as send_brevo_email:
            result = process_pending_queue_emails()

        delivery.refresh_from_db()
        self.assertEqual(result, {"processed": 1, "sent": 1, "retrying": 0, "failed": 0})
        self.assertEqual(delivery.status, QueueEmailDelivery.STATUS_SENT)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertIsNotNone(delivery.sent_at)
        send_brevo_email.assert_called_once()
        self.assertEqual(
            send_brevo_email.call_args.kwargs["recipient"], self.customer.email
        )

    def test_management_command_sends_one_batch_and_exits(self):
        self.join(self.customer_client)
        output = StringIO()

        with patch("user.services.notifications.send_brevo_email"):
            call_command("send_queue_emails", once=True, stdout=output)

        delivery = QueueEmailDelivery.objects.get(event="joined")
        self.assertEqual(delivery.status, QueueEmailDelivery.STATUS_SENT)
        self.assertIn("sent=1", output.getvalue())

    def test_email_failure_retries_then_fails_after_limited_attempts(self):
        self.join(self.customer_client)
        delivery = QueueEmailDelivery.objects.get(event="joined")
        now = timezone.now() + timedelta(seconds=1)

        with patch(
            "user.services.notifications.send_brevo_email",
            side_effect=BrevoEmailError("Brevo HTTP 503"),
        ) as send_brevo_email:
            for attempt in range(1, MAX_EMAIL_ATTEMPTS + 1):
                result = process_pending_queue_emails(now=now)
                delivery.refresh_from_db()
                self.assertEqual(delivery.attempt_count, attempt)
                self.assertIn(delivery.status, ("pending", "failed"))
                if attempt < MAX_EMAIL_ATTEMPTS:
                    self.assertEqual(result["retrying"], 1)
                    self.assertEqual(delivery.status, QueueEmailDelivery.STATUS_PENDING)
                    now = delivery.next_attempt_at + timedelta(seconds=1)
                else:
                    self.assertEqual(result["failed"], 1)
                    self.assertEqual(delivery.status, QueueEmailDelivery.STATUS_FAILED)

        self.assertEqual(send_brevo_email.call_count, MAX_EMAIL_ATTEMPTS)
        self.assertIn("Brevo HTTP 503", delivery.last_error)

    def test_duplicate_event_key_returns_one_delivery(self):
        self.join(self.customer_client)
        queue = Queue.objects.get(user=self.customer)
        existing = QueueEmailDelivery.objects.get(event="joined")

        duplicate = enqueue_queue_email(
            queue,
            "joined",
            "Queue joined",
            f"Your token {queue.token} has been added to the {queue.service} queue.",
        )

        self.assertEqual(duplicate.pk, existing.pk)
        self.assertEqual(QueueEmailDelivery.objects.filter(event_key=existing.event_key).count(), 1)

    def test_queue_transaction_rollback_discards_email_outbox_and_in_app_record(self):
        from user.services.notifications import enqueue_queue_email as real_enqueue

        def enqueue_then_fail(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            raise RuntimeError("force transaction rollback")

        with patch("user.services.notifications.send_brevo_email") as send_brevo_email:
            with patch("user.views.queue.enqueue_queue_email", side_effect=enqueue_then_fail):
                with self.assertRaisesRegex(RuntimeError, "force transaction rollback"):
                    self.join(self.customer_client)
            process_pending_queue_emails()
            send_brevo_email.assert_not_called()

        self.assertFalse(Queue.objects.filter(user=self.customer).exists())
        self.assertFalse(CustomerNotification.objects.filter(user=self.customer).exists())
        self.assertFalse(QueueEmailDelivery.objects.filter(user=self.customer).exists())
