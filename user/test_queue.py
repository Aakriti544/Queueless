import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from user.models import Queue, QueueTokenCounter, Service, User


class CustomerQueueTests(TransactionTestCase):
    reset_sequences = True

    @staticmethod
    def make_user(username, role="customer", active=True):
        return User.objects.create(
            username=username,
            email=f"{username}@queue-test.example",
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
    def join(client, service="General"):
        service_record, _ = Service.objects.get_or_create(name=service)
        return client.post(
            "/api/user/join-queue/",
            data=json.dumps({"service_id": service_record.pk}),
            content_type="application/json",
        )

    def setUp(self):
        # TransactionTestCase flushes application data between methods; recreate
        # the singleton row normally seeded by migration 0011.
        QueueTokenCounter.objects.get_or_create(
            name="main",
            defaults={"queue_date": timezone.localdate(), "last_token_number": 0},
        )

    def test_customer_gets_token_at_join_from_shared_sequence_across_services(self):
        first = self.make_user("join-one")
        second = self.make_user("join-two")
        one = self.join(self.client_for(first), "Account Service")
        second_client = self.client_for(second)
        two = self.join(second_client, "Loan Service")

        self.assertEqual(one.status_code, 201, one.content)
        self.assertEqual(two.status_code, 201, two.content)
        self.assertEqual(one.json()["token_number"], 1)
        self.assertEqual(two.json()["token_number"], 2)
        self.assertEqual(one.json()["queue"]["position"], 1)
        self.assertEqual(two.json()["queue"]["position"], 2)
        self.assertEqual(two.json()["queue"]["estimatedWaitMinutes"], 20)
        self.assertIsNotNone(two.json()["queue"]["estimatedTurnAt"])
        self.assertIsNotNone(two.json()["queue"]["lastUpdatedAt"])
        status_response = second_client.get("/api/user/my-queue/")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(
            status_response.json()["queue"]["estimatedWaitMinutes"],
            two.json()["queue"]["estimatedWaitMinutes"],
        )
        entry = Queue.objects.get(pk=one.json()["queue"]["id"])
        self.assertEqual(entry.queue_date, timezone.localdate())

    def test_daily_counter_resets_in_kathmandu_and_unique_tokens_are_date_scoped(self):
        first_day = date(2026, 11, 1)
        next_day = date(2026, 11, 2)
        customer_one = self.make_user("daily-one")
        customer_two = self.make_user("daily-two")
        customer_three = self.make_user("daily-three")

        with patch("user.services.queue.timezone.localdate", return_value=first_day):
            first = self.join(self.client_for(customer_one))
            second = self.join(self.client_for(customer_two))
        with patch("user.services.queue.timezone.localdate", return_value=next_day):
            next_day_response = self.join(self.client_for(customer_three))

        self.assertEqual([first.json()["token_number"], second.json()["token_number"]], [1, 2])
        self.assertEqual(next_day_response.json()["token_number"], 1)
        self.assertEqual(
            list(Queue.objects.order_by("queue_date", "token_number").values_list("queue_date", "token_number")),
            [(first_day, 1), (first_day, 2), (next_day, 1)],
        )
        counter = QueueTokenCounter.objects.get(name="main")
        self.assertEqual(counter.queue_date, next_day)
        self.assertEqual(counter.last_token_number, 1)

    def test_daily_token_rollover_uses_kathmandu_local_midnight(self):
        before_midnight_utc = [datetime(2026, 11, 1, 18, 14, tzinfo=datetime_timezone.utc)]
        customer_one = self.make_user("kathmandu-day-one")
        customer_two = self.make_user("kathmandu-day-two")

        with timezone.override("Asia/Kathmandu"):
            with patch(
                "user.services.queue.timezone.now",
                side_effect=lambda: before_midnight_utc[0],
            ):
                first = self.join(self.client_for(customer_one))
                before_midnight_utc[0] = datetime(
                    2026, 11, 1, 18, 15, tzinfo=datetime_timezone.utc
                )
                second = self.join(self.client_for(customer_two))

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()["token_number"], 1)
        self.assertEqual(second.json()["token_number"], 1)
        self.assertEqual(
            list(Queue.objects.order_by("queue_date").values_list("queue_date", "token_number")),
            [(date(2026, 11, 1), 1), (date(2026, 11, 2), 1)],
        )
        counter = QueueTokenCounter.objects.get(name="main")
        self.assertEqual(counter.queue_date, date(2026, 11, 2))
        self.assertEqual(counter.last_token_number, 1)

    def test_customer_can_have_only_one_active_entry_and_cancel_releases_it(self):
        customer = self.make_user("one-active")
        client = self.client_for(customer)
        first = self.join(client, "First Service")
        duplicate = self.join(client, "Different Service")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(first.json()["queue"]["id"], duplicate.json()["queue"]["id"])
        self.assertEqual(Queue.objects.filter(active_customer=customer).count(), 1)

        response = client.post("/api/user/leave-queue/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Queue.objects.filter(active_customer=customer).count(), 0)
        rejoin = self.join(client, "Second Visit")
        self.assertEqual(rejoin.status_code, 201)
        self.assertNotEqual(first.json()["queue"]["id"], rejoin.json()["queue"]["id"])

    def test_database_constraint_prevents_two_active_entries(self):
        customer = self.make_user("db-guard")
        Queue.objects.create(user=customer, service="One", token_number=51)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Queue.objects.create(user=customer, service="Two", token_number=52)

    def test_terminal_staff_status_releases_active_entry(self):
        customer = self.make_user("terminal-guard")
        entry = Queue.objects.create(user=customer, service="General", token_number=61)
        entry.status = "completed"
        entry.save(update_fields=["status", "updated_at"])
        entry.refresh_from_db()
        self.assertIsNone(entry.active_customer_id)
        self.assertFalse(Queue.objects.filter(active_customer=customer).exists())

    def test_estimated_wait_uses_the_organization_wide_waiting_order(self):
        first = self.make_user("estimate-one")
        second = self.make_user("estimate-two")
        self.join(self.client_for(first), "Service A")
        self.join(self.client_for(second), "Service B")
        response = self.client_for(second).get("/api/user/my-queue/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queue"]["position"], 2)
        self.assertEqual(response.json()["queue"]["estimatedWait"], "20 min")

    @override_settings(CUSTOMER_WAIT_ESTIMATE_MINUTES=15)
    def test_wait_estimate_uses_central_configured_baseline(self):
        first = self.make_user("configured-estimate-one")
        second = self.make_user("configured-estimate-two")
        self.join(self.client_for(first), "Service A")
        response = self.join(self.client_for(second), "Service B")
        self.assertEqual(response.json()["queue"]["estimatedWait"], "30 min")

    def test_wait_estimate_accounts_for_active_staff_service_duration(self):
        in_service = self.make_user("estimating-in-service")
        waiting = self.make_user("estimating-waiting")
        staff = self.make_user("estimating-staff", role="staff")
        Queue.objects.create(
            user=in_service,
            called_by=staff,
            service="Existing",
            service_duration_minutes=15,
            status="serving",
        )
        response = self.join(self.client_for(waiting), "Next")
        self.assertEqual(response.json()["queue"]["estimatedWait"], "15 min")

    def test_called_service_duration_and_waiting_ahead_duration_are_counted(self):
        called_customer = self.make_user("estimate-called-customer")
        waiting_ahead = self.make_user("estimate-duration-ahead")
        waiting_target = self.make_user("estimate-duration-target")
        staff = self.make_user("estimate-duration-staff", role="staff")
        Queue.objects.create(
            user=called_customer,
            called_by=staff,
            service="Called service",
            service_duration_minutes=10,
            status="called",
        )
        ahead = Queue.objects.create(
            user=waiting_ahead,
            service="Long expected service",
            service_duration_minutes=7,
            status="waiting",
        )
        Queue.objects.filter(pk=ahead.pk).update(created_at=timezone.now() - timedelta(minutes=2))

        response = self.join(self.client_for(waiting_target), "Target service")

        # The assigned call contributes 10 minutes and the earlier waiting
        # entry contributes its saved seven-minute expected duration.
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["queue"]["estimatedWait"], "17 min")

    def test_wait_estimate_balances_active_service_durations_across_staff(self):
        staff_one = self.make_user("estimate-staff-one", role="staff")
        staff_two = self.make_user("estimate-staff-two", role="staff")
        customer_one = self.make_user("estimate-customer-one")
        customer_two = self.make_user("estimate-customer-two")
        waiting = self.make_user("estimate-customer-waiting")
        Queue.objects.create(
            user=customer_one,
            called_by=staff_one,
            service="Long service",
            service_duration_minutes=30,
            status="serving",
        )
        Queue.objects.create(
            user=customer_two,
            called_by=staff_two,
            service="Short service",
            service_duration_minutes=8,
            status="serving",
        )

        response = self.join(self.client_for(waiting), "Next")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["queue"]["estimatedWait"], "8 min")

    def test_wait_uses_remaining_service_time_then_releases_it_on_completion(self):
        first = self.make_user("estimate-active-first")
        waiting = self.make_user("estimate-active-waiting")
        staff = self.make_user("estimate-active-staff", role="staff")
        first_client = self.client_for(first)
        waiting_client = self.client_for(waiting)
        staff_client = self.client_for(staff)

        joined_first = self.join(first_client)
        self.assertEqual(joined_first.status_code, 201)
        called = staff_client.post("/api/user/call-next/", data="{}", content_type="application/json")
        self.assertEqual(called.status_code, 200, called.content)
        queue_id = joined_first.json()["queue"]["id"]
        started = staff_client.post(
            "/api/user/start-service/",
            data=json.dumps({"queue_id": queue_id, "service_duration_minutes": 10}),
            content_type="application/json",
        )
        self.assertEqual(started.status_code, 200, started.content)

        joined_waiting = self.join(waiting_client)
        self.assertEqual(joined_waiting.json()["queue"]["estimatedWait"], "10 min")
        from user.services.queue import calculate_customer_wait_time

        now = timezone.now()
        waiting_entry = Queue.objects.get(pk=joined_waiting.json()["queue"]["id"])
        self.assertEqual(
            calculate_customer_wait_time(waiting_entry, now=now + timedelta(minutes=3)),
            7,
        )

        completed = staff_client.post(
            "/api/user/complete-token/",
            data=json.dumps({"queue_id": queue_id}),
            content_type="application/json",
        )
        self.assertEqual(completed.status_code, 200, completed.content)
        refreshed = waiting_client.get("/api/user/my-queue/")
        self.assertEqual(refreshed.json()["queue"]["estimatedWait"], "0 min")

    def test_skipping_called_customer_removes_their_workload_for_waiting_customer(self):
        first = self.make_user("estimate-skip-first")
        waiting = self.make_user("estimate-skip-waiting")
        staff = self.make_user("estimate-skip-staff", role="staff")
        first_client = self.client_for(first)
        waiting_client = self.client_for(waiting)
        staff_client = self.client_for(staff)
        first_join = self.join(first_client)
        self.assertEqual(staff_client.post("/api/user/call-next/", data="{}", content_type="application/json").status_code, 200)
        waiting_join = self.join(waiting_client)
        self.assertEqual(waiting_join.json()["queue"]["estimatedWait"], "10 min")

        queue_id = first_join.json()["queue"]["id"]
        Queue.objects.filter(pk=queue_id).update(grace_deadline=timezone.now() - timedelta(seconds=1))
        skipped = staff_client.post(
            "/api/user/skip-token/",
            data=json.dumps({"queue_id": queue_id}),
            content_type="application/json",
        )
        self.assertEqual(skipped.status_code, 200, skipped.content)
        refreshed = waiting_client.get("/api/user/my-queue/")
        self.assertEqual(refreshed.json()["queue"]["estimatedWait"], "0 min")

    def test_customer_services_endpoint_is_role_protected(self):
        customer = self.make_user("service-reader")
        Service.objects.create(name="Available", is_active=True)
        Service.objects.create(name="Unavailable", is_active=False)
        response = self.client_for(customer).get("/api/user/customer-services/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s["name"] for s in response.json()["services"]], ["Available"])

        staff = self.make_user("service-staff", "staff")
        denied = self.client_for(staff).get("/api/user/customer-services/")
        self.assertEqual(denied.status_code, 403)

    def test_inactive_customer_cannot_join(self):
        customer = self.make_user("inactive-customer", active=False)
        client = self.client_for(customer)
        response = self.join(client)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Queue.objects.count(), 0)

    def test_concurrent_customers_get_distinct_daily_tokens(self):
        users = [self.make_user(f"parallel-{i}") for i in range(4)]
        barrier = Barrier(len(users))

        def do_join(user_id):
            close_old_connections()
            user = User.objects.get(pk=user_id)
            client = self.client_for(user)
            barrier.wait(timeout=10)
            response = self.join(client, f"Service {user_id}")
            result = (response.status_code, response.json())
            close_old_connections()
            return result

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            results = list(pool.map(do_join, [user.id for user in users]))

        self.assertEqual([status for status, _ in results], [201] * len(users), results)
        tokens = sorted(body["token_number"] for _, body in results)
        self.assertEqual(tokens, [1, 2, 3, 4])
        self.assertEqual(Queue.objects.count(), 4)

    def test_concurrent_same_customer_join_reuses_one_active_entry(self):
        customer = self.make_user("parallel-same-user")
        barrier = Barrier(2)

        def do_join(service):
            close_old_connections()
            user = User.objects.get(pk=customer.id)
            client = self.client_for(user)
            barrier.wait(timeout=10)
            response = self.join(client, service)
            result = (response.status_code, response.json())
            close_old_connections()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(do_join, ("Concurrent A", "Concurrent B")))

        self.assertEqual(sorted(status for status, _ in results), [200, 201], results)
        self.assertEqual(
            len({body["queue"]["id"] for _, body in results}), 1
        )
        self.assertEqual(Queue.objects.filter(active_customer=customer).count(), 1)

