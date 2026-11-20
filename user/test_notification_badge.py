from django.test import Client, TestCase

from user.models import CustomerNotification, User


class NotificationUnreadCountTests(TestCase):
    def make_user(self, role):
        ordinal = User.objects.filter(role=role).count()
        return User.objects.create(
            username=f"{role} account {ordinal}",
            email=f"{role}-{ordinal}@example.test",
            password="unused",
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

    def test_unread_count_is_database_backed_and_scoped_to_each_role_account(self):
        customer = self.make_user("customer")
        staff = self.make_user("staff")
        admin = self.make_user("admin")
        other_customer = self.make_user("customer")

        for owner in (customer, customer, staff, admin, other_customer):
            CustomerNotification.objects.create(
                user=owner, title="Queue update", message="Status changed"
            )
        CustomerNotification.objects.filter(user=customer).first().delete()

        for account, expected in ((customer, 1), (staff, 1), (admin, 1), (other_customer, 1)):
            response = self.client_for(account).get("/api/user/notification-unread-count/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"success": True, "unread_count": expected})

        CustomerNotification.objects.filter(user=customer, is_read=False).update(is_read=True)
        response = self.client_for(customer).get("/api/user/notification-unread-count/")
        self.assertEqual(response.json()["unread_count"], 0)

    def test_unread_count_requires_a_valid_signed_in_session(self):
        self.assertEqual(
            Client().get("/api/user/notification-unread-count/").status_code,
            401,
        )

    def test_customer_notifications_poll_includes_count_with_incremental_cursor(self):
        customer = self.make_user("customer")
        client = self.client_for(customer)
        CustomerNotification.objects.create(
            user=customer, title="One", message="First"
        )
        CustomerNotification.objects.create(
            user=customer, title="Two", message="Second", is_read=True
        )
        response = client.get("/api/user/customer-notifications/?after_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unread_count"], 1)
        self.assertEqual([n["title"] for n in response.json()["notifications"]], ["Two"])
