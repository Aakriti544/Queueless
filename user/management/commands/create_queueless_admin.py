from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from user.models import User
from django.conf import settings


class Command(BaseCommand):
    help = "Create or update the initial QueueLess admin."

    def handle(self, *args, **options):
        username = settings.ADMIN_USERNAME
        email = (settings.ADMIN_EMAIL or "").strip().lower()
        password = settings.ADMIN_PASSWORD

        if not username or not email or not password:
            raise CommandError(
                "ADMIN_USERNAME, ADMIN_EMAIL and ADMIN_PASSWORD "
                "must be configured in .env"
            )

        with transaction.atomic():
            users = User.objects.select_for_update().filter(email__iexact=email)

            if users.count() > 1:
                raise CommandError(
                    "Multiple accounts use this email. Resolve duplicates first."
                )

            admin = users.first()

            if admin:
                admin.username = username
                admin.email = email
                admin.password = make_password(password)
                admin.role = "admin"
                admin.is_active = True
                admin.save()

                self.stdout.write(
                    self.style.SUCCESS(
                        "Existing account updated and promoted to admin."
                    )
                )
            else:
                User.objects.create(
                    username=username,
                    email=email,
                    password=make_password(password),
                    role="admin",
                    is_active=True,
                )

                self.stdout.write(
                    self.style.SUCCESS("New QueueLess admin created successfully.")
                )

        self.stdout.write("QueueLess admin setup completed.")
