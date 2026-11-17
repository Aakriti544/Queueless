import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.db import close_old_connections
from django.test import Client, TransactionTestCase
from django.utils import timezone

from user.models import Queue, User


class StaffQueueTests(TransactionTestCase):
    reset_sequences = True

    @staticmethod
    def make_user(username, role, active=True):
        return User.objects.create(
            username=username,
            email=f"{username}@staff-test.example",
            password="test-hash",
            role=role,
            is_active=active,
        )

    @staticmethod
    def client_for(user):
        client = Client()
        session = client.session
        session["user_id"] = user.id
        session["user_role"] = user.role
        session["is_logged_in"] = True
        session.save()
        return client

    @staticmethod
    def call(client):
        return client.post(
            "/api/user/call-next/",
            data="{}",
            content_type="application/json",
        )

    @staticmethod
    def queue_entry(customer, token_number):
        return Queue.objects.create(
            user=customer,
            service="General",
            token_number=token_number,
            status="waiting",
        )

    def setUp(self):
        self.staff_one = self.make_user("staff-one", "staff")
        self.staff_two = self.make_user("staff-two", "staff")
        self.customer_one = self.make_user("customer-one", "customer")
        self.customer_two = self.make_user("customer-two", "customer")

    def test_call_records_staff_and_exact_two_minute_deadline(self):
        entry = self.queue_entry(self.customer_one, 401)
        response = self.call(self.client_for(self.staff_one))
        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        self.assertEqual(entry.called_by_id, self.staff_one.id)
        self.assertEqual(entry.status, "called")
        self.assertIsNotNone(entry.called_at)
        self.assertEqual(
            (entry.grace_deadline - entry.called_at).total_seconds(), 120
        )
        self.assertEqual(response.json()["queue"]["called_by_id"], self.staff_one.id)

    def test_different_staff_can_claim_different_entries(self):
        first = self.queue_entry(self.customer_one, 402)
        second = self.queue_entry(self.customer_two, 403)
        response_one = self.call(self.client_for(self.staff_one))
        response_two = self.call(self.client_for(self.staff_two))
        self.assertEqual(response_one.status_code, 200, response_one.content)
        self.assertEqual(response_two.status_code, 200, response_two.content)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, "called")
        self.assertEqual(second.status, "called")
        self.assertNotEqual(first.called_by_id, second.called_by_id)

    def test_simultaneous_staff_calls_claim_distinct_entries(self):
        first = self.queue_entry(self.customer_one, 404)
        second = self.queue_entry(self.customer_two, 405)
        barrier = Barrier(2)

        def run_call(staff_id):
            close_old_connections()
            staff = User.objects.get(pk=staff_id)
            client = self.client_for(staff)
            barrier.wait(timeout=10)
            response = self.call(client)
            result = (response.status_code, response.json())
            close_old_connections()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_call, (self.staff_one.id, self.staff_two.id)))

        self.assertEqual([code for code, _ in results], [200, 200], results)
        ids = [body["queue"]["id"] for _, body in results]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(
            {body["queue"]["called_by_id"] for _, body in results},
            {self.staff_one.id, self.staff_two.id},
        )
        for entry in (first, second):
            entry.refresh_from_db()
            self.assertEqual(entry.status, "called")
            self.assertIn(entry.called_by_id, {self.staff_one.id, self.staff_two.id})
            self.assertIsNotNone(entry.called_at)
        self.assertEqual(Queue.objects.filter(status="called").count(), 2)

    def test_same_staff_cannot_claim_multiple_active_customers(self):
        self.queue_entry(self.customer_one, 406)
        self.queue_entry(self.customer_two, 407)
        client = self.client_for(self.staff_one)
        self.assertEqual(self.call(client).status_code, 200)
        response = self.call(client)
        self.assertEqual(response.status_code, 409)

    def test_skip_is_manual_and_rejected_before_grace_deadline(self):
        entry = self.queue_entry(self.customer_one, 408)
        client = self.client_for(self.staff_one)
        self.assertEqual(self.call(client).status_code, 200)
        response = client.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "called")

        entry.grace_deadline = timezone.now() - timedelta(seconds=1)
        entry.save(update_fields=["grace_deadline"])
        response = client.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "skipped")

    def test_started_service_cannot_be_skipped_and_can_be_completed(self):
        entry = self.queue_entry(self.customer_one, 409)
        client = self.client_for(self.staff_one)
        self.assertEqual(self.call(client).status_code, 200)

        response = client.post(
            "/api/user/start-service/",
            data=json.dumps({"queue_id": entry.id, "service_duration_minutes": 15}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["queue"]["status"], "serving")
        self.assertEqual(response.json()["queue"]["service_duration_minutes"], 15)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "serving")
        self.assertEqual(entry.service_duration_minutes, 15)

        response = client.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

        response = client.post(
            "/api/user/complete-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "completed")

    def test_inactive_staff_cannot_read_or_mutate_queue(self):
        entry = self.queue_entry(self.customer_one, 410)
        self.staff_one.is_active = False
        self.staff_one.save(update_fields=["is_active"])
        client = self.client_for(self.staff_one)
        self.assertEqual(client.get("/api/user/staff-queue/").status_code, 403)
        self.assertEqual(self.call(client).status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "waiting")

    def test_staff_cannot_mutate_another_staffs_assigned_customer(self):
        entry = self.queue_entry(self.customer_one, 413)
        owner = self.client_for(self.staff_one)
        other = self.client_for(self.staff_two)
        self.assertEqual(self.call(owner).status_code, 200)

        start = other.post(
            "/api/user/start-service/",
            data=json.dumps({"queue_id": entry.id, "service_duration_minutes": 15}),
            content_type="application/json",
        )
        complete = other.post(
            "/api/user/complete-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        entry.grace_deadline = timezone.now() - timezone.timedelta(seconds=1)
        entry.save(update_fields=["grace_deadline", "updated_at"])
        skip = other.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": entry.id}),
            content_type="application/json",
        )
        self.assertEqual([start.status_code, complete.status_code, skip.status_code], [403, 403, 403])
        entry.refresh_from_db()
        self.assertEqual(entry.status, "called")

    def test_staff_queue_exposes_shared_entries_and_staff_attribution(self):
        first = self.queue_entry(self.customer_one, 411)
        second = self.queue_entry(self.customer_two, 412)
        self.call(self.client_for(self.staff_one))
        self.call(self.client_for(self.staff_two))
        response = self.client_for(self.staff_one).get("/api/user/staff-queue/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["queues"]), 2)
        self.assertEqual(len(data["my_calls"]), 1)
        self.assertEqual(data["my_calls"][0]["id"], first.id)
        self.assertEqual(data["queues"][1]["called_by_id"], self.staff_two.id)

