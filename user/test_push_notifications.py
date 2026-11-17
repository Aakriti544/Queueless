import base64
import json
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.db.models import Count
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from user.models import (
    CustomerNotification,
    PushSubscription,
    Queue,
    QueueEmailDelivery,
    QueuePushDelivery,
    QueueTokenCounter,
    Service,
    User,
)
from user.services.push_delivery import MAX_PUSH_ATTEMPTS, process_pending_queue_pushes
from user.services.wait_notifications import process_waiting_notifications


_PUBLIC_KEY = base64.urlsafe_b64encode(b"\x04" + b"p" * 64).rstrip(b"=").decode()
_ENCRYPTION_KEY = Fernet.generate_key().decode()


@override_settings(
    WEB_PUSH_VAPID_PUBLIC_KEY=_PUBLIC_KEY,
    WEB_PUSH_VAPID_PRIVATE_KEY="test-private-key",
    WEB_PUSH_VAPID_SUBJECT="mailto:push@example.test",
    WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY=_ENCRYPTION_KEY,
)
class PushNotificationTests(TestCase):
    def setUp(self):
        QueueTokenCounter.objects.get_or_create(name="main")
        self.service = Service.objects.create(name="Push Test Service")
        self.customer = self.make_user("push-customer", "customer")
        self.staff = self.make_user("push-staff", "staff")
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

    @staticmethod
    def subscription_payload(device):
        auth = base64.urlsafe_b64encode(f"auth-{device}".encode()).rstrip(b"=").decode()
        p256dh = base64.urlsafe_b64encode(f"p256dh-{device}".encode()).rstrip(b"=").decode()
        return {
            "endpoint": f"https://fcm.googleapis.com/wp/queueless-test-{device}",
            "keys": {"auth": auth, "p256dh": p256dh},
        }

    def register(self, client, device):
        return client.post(
            "/api/user/customer-push-subscriptions/",
            data=json.dumps({"subscription": self.subscription_payload(device)}),
            content_type="application/json",
        )

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

    def test_subscription_registration_is_private_and_supports_multiple_devices(self):
        first = self.register(self.customer_client, "one")
        duplicate = self.register(self.customer_client, "one")
        second = self.register(self.customer_client, "two")

        self.assertEqual([first.status_code, duplicate.status_code, second.status_code], [201, 200, 201])
        self.assertEqual(PushSubscription.objects.filter(user=self.customer).count(), 2)
        status = self.customer_client.get("/api/user/customer-push-subscriptions/")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["configured"])
        self.assertEqual(status.json()["device_count"], 2)
        self.assertEqual(status.json()["vapid_public_key"], _PUBLIC_KEY)
        self.assertNotIn("endpoint", json.dumps(status.json()))
        self.assertNotIn("auth", json.dumps(status.json()))
        for subscription in PushSubscription.objects.filter(user=self.customer):
            self.assertNotIn("fcm.googleapis.com", subscription.encrypted_subscription)
            self.assertNotIn("p256dh", subscription.encrypted_subscription)

    def test_subscription_removal_only_removes_the_calling_customer_device(self):
        self.register(self.customer_client, "remove-one")
        self.register(self.customer_client, "keep-two")
        payload = self.subscription_payload("remove-one")
        removed = self.customer_client.delete(
            "/api/user/customer-push-subscriptions/",
            data=json.dumps({"subscription": payload}),
            content_type="application/json",
        )

        self.assertEqual(removed.status_code, 200, removed.content)
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(removed.json()["device_count"], 1)
        self.assertEqual(PushSubscription.objects.filter(user=self.customer).count(), 1)

    def test_registration_requires_customer_and_rejects_untrusted_endpoint(self):
        staff_response = self.register(self.staff_client, "staff-device")
        self.assertEqual(staff_response.status_code, 403)
        invalid = self.customer_client.post(
            "/api/user/customer-push-subscriptions/",
            data=json.dumps(
                {
                    "subscription": {
                        **self.subscription_payload("invalid"),
                        "endpoint": "https://127.0.0.1/admin",
                    }
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(PushSubscription.objects.count(), 0)
        self.assertEqual(
            Client().get("/api/user/customer-push-subscriptions/").status_code, 401
        )

    def test_unconfigured_push_rejects_enable_request_without_affecting_email(self):
        with override_settings(
            WEB_PUSH_VAPID_PUBLIC_KEY="",
            WEB_PUSH_VAPID_PRIVATE_KEY="",
            WEB_PUSH_VAPID_SUBJECT="",
            WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY="",
        ):
            response = self.register(self.customer_client, "not-configured")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_all_queue_events_create_push_per_enabled_device_without_duplicate_in_app_or_email(self):
        main_devices = ("main-one", "main-two")
        for device in main_devices:
            self.register(self.customer_client, device)

        joined = self.join(self.customer_client)
        self.assertEqual(joined.status_code, 201, joined.content)
        queue_id = joined.json()["queue"]["id"]
        joined_push = QueuePushDelivery.objects.filter(
            queue_event__queue_id=queue_id, queue_event__event="joined"
        ).first()
        self.assertEqual(joined_push.title, "Queue joined")
        self.assertIn(joined.json()["queue"]["token"], joined_push.body)
        self.assertIn(self.service.name, joined_push.body)
        duplicate_join = self.join(self.customer_client)
        self.assertEqual(duplicate_join.status_code, 200)
        self.assertEqual(QueuePushDelivery.objects.count(), 2)

        self.assertEqual(self.call_next().status_code, 200)
        called_push = QueuePushDelivery.objects.filter(
            queue_event__queue_id=queue_id, queue_event__event="called"
        ).first()
        self.assertEqual(called_push.title, "Your turn is called")
        self.assertIn(joined.json()["queue"]["token"], called_push.body)
        started = self.staff_client.post(
            "/api/user/start-service/",
            data=json.dumps({"queue_id": queue_id, "service_duration_minutes": 10}),
            content_type="application/json",
        )
        self.assertEqual(started.status_code, 200, started.content)
        completed = self.staff_client.post(
            "/api/user/complete-token/",
            data=json.dumps({"queue_id": queue_id}),
            content_type="application/json",
        )
        self.assertEqual(completed.status_code, 200, completed.content)

        cancelled_customer = self.make_user("push-cancelled", "customer")
        cancelled_client = self.client_for(cancelled_customer)
        for device in ("cancel-one", "cancel-two"):
            self.register(cancelled_client, device)
        self.assertEqual(self.join(cancelled_client).status_code, 201)
        self.assertEqual(cancelled_client.post("/api/user/leave-queue/").status_code, 200)
        cancelled_queue = Queue.objects.get(user=cancelled_customer)
        cancelled_push = QueuePushDelivery.objects.filter(
            queue_event__queue=cancelled_queue, queue_event__event="cancelled"
        ).first()
        self.assertEqual(cancelled_push.title, "Queue cancelled")
        self.assertIn(cancelled_queue.token, cancelled_push.body)

        skipped_customer = self.make_user("push-skipped", "customer")
        skipped_client = self.client_for(skipped_customer)
        for device in ("skip-one", "skip-two"):
            self.register(skipped_client, device)
        skipped_join = self.join(skipped_client)
        self.assertEqual(skipped_join.status_code, 201)
        self.assertEqual(self.call_next().status_code, 200)
        skipped_id = skipped_join.json()["queue"]["id"]
        Queue.objects.filter(pk=skipped_id).update(
            grace_deadline=timezone.now() - timedelta(seconds=1)
        )
        skipped = self.staff_client.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": skipped_id}),
            content_type="application/json",
        )
        self.assertEqual(skipped.status_code, 200, skipped.content)

        self.assertEqual(QueueEmailDelivery.objects.count(), 9)
        self.assertEqual(CustomerNotification.objects.count(), 9)
        self.assertEqual(QueuePushDelivery.objects.count(), 18)
        self.assertEqual(
            dict(
                QueuePushDelivery.objects.values("queue_event__event")
                .annotate(count=Count("id"))
                .values_list("queue_event__event", "count")
            ),
            {
                "joined": 6,
                "called": 4,
                "started": 2,
                "completed": 2,
                "cancelled": 2,
                "skipped": 2,
            },
        )

    def test_push_sends_queue_event_payload_after_customer_logs_out(self):
        self.register(self.customer_client, "logged-out-device")
        joined = self.join(self.customer_client)
        self.assertEqual(joined.status_code, 201)
        joined_push = QueuePushDelivery.objects.get(queue_event__event="joined")
        self.assertIn(self.service.name, joined_push.body)
        self.customer_client.session.flush()
        self.assertEqual(self.call_next().status_code, 200)

        with patch("user.services.push_delivery.send_web_push") as send_push:
            result = process_pending_queue_pushes()

        self.assertEqual(result["sent"], 2)
        self.assertEqual(send_push.call_count, 2)
        payload = send_push.call_args.args[1]
        self.assertEqual(payload["title"], "Your turn is called")
        self.assertIn("A001", payload["body"])
        self.assertNotIn(self.customer.username, json.dumps(payload))
        self.assertNotIn(self.customer.email, json.dumps(payload))
        self.assertEqual(QueuePushDelivery.objects.filter(status="sent").count(), 2)

    def test_waiting_milestones_use_existing_push_outbox_without_duplicate_event(self):
        self.register(self.customer_client, "wait-reminder-device")
        serving_customer = self.make_user("push-serving", "customer")
        Queue.objects.create(
            user=serving_customer,
            called_by=self.staff,
            service="Long service",
            status="serving",
            service_duration_minutes=10,
        )
        joined = self.join(self.customer_client)
        entry = Queue.objects.get(pk=joined.json()["queue"]["id"])

        process_waiting_notifications(now=timezone.now() + timedelta(minutes=6))

        reminder_pushes = QueuePushDelivery.objects.filter(
            queue_event__queue=entry,
            queue_event__event__in=("periodic", "approaching", "next"),
        )
        self.assertEqual(reminder_pushes.count(), 3)
        self.assertEqual(
            CustomerNotification.objects.filter(user=self.customer).count(), 5
        )

    def test_exactly_three_people_ahead_creates_one_push_event(self):
        self.register(self.customer_client, "three-ahead-device")
        for index in range(3):
            earlier_customer = self.make_user(f"three-ahead-earlier-{index}", "customer")
            Queue.objects.create(user=earlier_customer, service="Earlier Service")

        joined = self.join(self.customer_client)
        entry = Queue.objects.get(pk=joined.json()["queue"]["id"])
        self.assertEqual(joined.json()["queue"]["peopleAhead"], 3)

        first = process_waiting_notifications()
        repeated = process_waiting_notifications()
        notice = QueueEmailDelivery.objects.get(queue=entry, event="three-ahead")
        push = QueuePushDelivery.objects.get(queue_event=notice)

        self.assertGreaterEqual(first["notices"], 1)
        self.assertEqual(repeated["notices"], 0)
        self.assertEqual(push.title, "3 people ahead")
        self.assertIn("3 people", push.body)

    def test_waiting_push_reminders_are_discarded_after_completion(self):
        self.register(self.customer_client, "stale-wait-device")
        joined = self.join(self.customer_client)
        entry = Queue.objects.get(pk=joined.json()["queue"]["id"])
        process_waiting_notifications(now=timezone.now() + timedelta(minutes=6))
        Queue.objects.filter(pk=entry.pk).update(status="completed", active_customer=None)
        QueuePushDelivery.objects.filter(queue_event__queue=entry, queue_event__event="joined").update(status="sent")

        with patch("user.services.push_delivery.send_web_push") as send_push:
            process_pending_queue_pushes()

        stale_reminders = QueuePushDelivery.objects.filter(
            queue_event__queue=entry,
            queue_event__event__in=("periodic", "approaching", "next", "estimate-change"),
        )
        self.assertTrue(stale_reminders.exists())
        self.assertFalse(stale_reminders.exclude(status=QueuePushDelivery.STATUS_FAILED).exists())
        send_push.assert_not_called()

    def test_expired_subscription_is_removed_and_delivery_marked_failed(self):
        self.register(self.customer_client, "expired-device")
        self.join(self.customer_client)
        delivery = QueuePushDelivery.objects.get()

        class GoneError(Exception):
            status_code = 410

        with patch(
            "user.services.push_delivery.send_web_push", side_effect=GoneError()
        ):
            result = process_pending_queue_pushes()

        delivery.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(delivery.status, QueuePushDelivery.STATUS_FAILED)
        self.assertIsNone(delivery.subscription_id)
        self.assertEqual(PushSubscription.objects.filter(user=self.customer).count(), 0)

    def test_invalid_subscription_is_removed_after_push_service_rejection(self):
        self.register(self.customer_client, "invalid-device")
        self.join(self.customer_client)
        delivery = QueuePushDelivery.objects.get()

        class InvalidSubscriptionError(Exception):
            status_code = 400

        with patch(
            "user.services.push_delivery.send_web_push",
            side_effect=InvalidSubscriptionError(),
        ):
            result = process_pending_queue_pushes()

        delivery.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(delivery.status, QueuePushDelivery.STATUS_FAILED)
        self.assertEqual(PushSubscription.objects.filter(user=self.customer).count(), 0)

    def test_push_failures_retry_a_limited_number_of_times(self):
        self.register(self.customer_client, "retry-device")
        self.join(self.customer_client)
        delivery = QueuePushDelivery.objects.get()
        now = timezone.now() + timedelta(seconds=1)

        with patch(
            "user.services.push_delivery.send_web_push",
            side_effect=OSError("push service unavailable"),
        ) as send_push:
            for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
                result = process_pending_queue_pushes(now=now)
                delivery.refresh_from_db()
                self.assertEqual(delivery.attempt_count, attempt)
                if attempt < MAX_PUSH_ATTEMPTS:
                    self.assertEqual(delivery.status, QueuePushDelivery.STATUS_PENDING)
                    self.assertEqual(result["retrying"], 1)
                    now = delivery.next_attempt_at + timedelta(seconds=1)
                else:
                    self.assertEqual(delivery.status, QueuePushDelivery.STATUS_FAILED)
                    self.assertEqual(result["failed"], 1)

        self.assertEqual(send_push.call_count, MAX_PUSH_ATTEMPTS)
        self.assertNotIn("push service unavailable", delivery.last_error)

    def test_rolled_back_queue_event_creates_no_push_or_email_delivery(self):
        self.register(self.customer_client, "rollback-device")
        from user.services.notifications import enqueue_queue_email as real_enqueue

        def enqueue_then_fail(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            raise RuntimeError("force rollback")

        with patch("user.views.queue.enqueue_queue_email", side_effect=enqueue_then_fail):
            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                self.join(self.customer_client)

        self.assertFalse(Queue.objects.filter(user=self.customer).exists())
        self.assertEqual(QueueEmailDelivery.objects.count(), 0)
        self.assertEqual(QueuePushDelivery.objects.count(), 0)
        self.assertEqual(CustomerNotification.objects.count(), 0)
