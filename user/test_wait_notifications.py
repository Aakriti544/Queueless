import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from user.models import (
    CustomerNotification,
    Queue,
    QueueEmailDelivery,
    QueueTokenCounter,
    QueueWaitNotice,
    Service,
    User,
)
from user.services.notifications import process_pending_queue_emails
from user.services.wait_notifications import process_waiting_notifications


class WaitingNotificationTests(TestCase):
    def setUp(self):
        QueueTokenCounter.objects.get_or_create(name="main")
        self.customer = self.make_user("wait-customer")
        self.staff = self.make_user("wait-staff", "staff")
        self.service = Service.objects.create(name="Waiting Service")
        self.client = self.client_for(self.customer)

    @staticmethod
    def make_user(username, role="customer"):
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
        session.update({"user_id": user.pk, "user_role": user.role, "is_logged_in": True})
        session.save()
        return client

    def join(self):
        return self.client.post(
            "/api/user/join-queue/",
            data=json.dumps({"service_id": self.service.pk}),
            content_type="application/json",
        )

    def test_join_includes_initial_wait_and_queue_api_has_kathmandu_display_fields(self):
        response = self.join()
        self.assertEqual(response.status_code, 201, response.content)
        queue_data = response.json()["queue"]
        self.assertEqual(queue_data["estimatedWaitMinutes"], 0)
        self.assertIn("+05:45", queue_data["lastUpdatedAt"])
        self.assertIsNotNone(queue_data["estimatedTurnAt"])
        delivery = QueueEmailDelivery.objects.get(event="joined")
        self.assertIn("initial estimated wait is about 0 minutes", delivery.message)
        self.assertTrue(QueueWaitNotice.objects.filter(event="baseline").exists())

        active = self.client.get("/api/user/my-queue/").json()["queue"]
        self.assertEqual(active["estimatedWaitMinutes"], 0)
        self.assertIn("lastUpdatedAt", active)

    def test_periodic_approaching_and_next_reminders_are_deduplicated(self):
        serving_customer = self.make_user("wait-serving")
        Queue.objects.create(
            user=serving_customer,
            called_by=self.staff,
            service="Long service",
            status="serving",
            service_duration_minutes=10,
        )
        response = self.join()
        entry = Queue.objects.get(pk=response.json()["queue"]["id"])
        now = timezone.now() + timedelta(minutes=6)

        first = process_waiting_notifications(now=now)
        repeated = process_waiting_notifications(now=now)

        events = QueueEmailDelivery.objects.filter(queue=entry).values_list("event", flat=True)
        self.assertEqual(list(events).count("periodic"), 1)
        self.assertEqual(list(events).count("approaching"), 1)
        self.assertEqual(list(events).count("next"), 1)
        self.assertGreaterEqual(first["notices"], 4, (first, list(events)))
        self.assertEqual(repeated["notices"], 0)
        self.assertEqual(
            CustomerNotification.objects.filter(user=self.customer).count(), 5
        )  # join notice plus changed, periodic, approaching and next notices

    def test_significant_estimate_change_uses_central_queue_calculation(self):
        response = self.join()
        entry = Queue.objects.get(pk=response.json()["queue"]["id"])
        earlier_customer = self.make_user("earlier-waiter")
        earlier = Queue.objects.create(user=earlier_customer, service="Earlier")
        Queue.objects.filter(pk=earlier.pk).update(
            created_at=entry.created_at - timedelta(seconds=1)
        )

        process_waiting_notifications(now=timezone.now() + timedelta(minutes=6))

        changed = QueueEmailDelivery.objects.filter(queue=entry, event="estimate-change")
        self.assertEqual(changed.count(), 1)
        self.assertIn("about 10 minutes", changed.get().message)

    def test_reminders_are_not_delivered_after_cancel_completion_or_skip(self):
        terminal_entries = []
        for index, terminal_status in enumerate(("cancelled", "completed", "skipped")):
            customer = self.make_user(f"terminal-wait-{index}")
            client = self.client_for(customer)
            response = client.post(
                "/api/user/join-queue/",
                data=json.dumps({"service_id": self.service.pk}),
                content_type="application/json",
            )
            terminal_entries.append(Queue.objects.get(pk=response.json()["queue"]["id"]))
        process_waiting_notifications(now=timezone.now() + timedelta(minutes=6))
        QueueEmailDelivery.objects.filter(event="joined").update(status="sent")
        for entry, terminal_status in zip(
            terminal_entries, ("cancelled", "completed", "skipped")
        ):
            Queue.objects.filter(pk=entry.pk).update(
                status=terminal_status, active_customer=None
            )
        with patch("user.services.notifications.send_brevo_email") as send_brevo_email:
            process_pending_queue_emails()

        reminders = QueueEmailDelivery.objects.filter(
            queue__in=terminal_entries,
            event__in=("periodic", "approaching", "next", "estimate-change"),
        )
        self.assertTrue(reminders.exists())
        self.assertFalse(reminders.exclude(status=QueueEmailDelivery.STATUS_FAILED).exists())
        send_brevo_email.assert_not_called()
