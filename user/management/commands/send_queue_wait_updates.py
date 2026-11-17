import time

from django.core.management.base import BaseCommand, CommandError

from user.services.wait_notifications import process_waiting_notifications


class Command(BaseCommand):
    help = "Schedule deduplicated waiting-time reminders into the notification outbox."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one pass and exit.")
        parser.add_argument("--poll-interval", type=int, default=30)
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        if options["poll_interval"] < 1 or options["batch_size"] < 1:
            raise CommandError("Poll interval and batch size must be positive.")
        self.stdout.write("Queue waiting notification scheduler started.")
        try:
            while True:
                result = process_waiting_notifications(batch_size=options["batch_size"])
                if result["notices"]:
                    self.stdout.write(
                        f"Scanned {result['processed']} queue entries; "
                        f"scheduled {result['notices']} notification events."
                    )
                if options["once"]:
                    break
                time.sleep(options["poll_interval"])
        except KeyboardInterrupt:
            self.stdout.write("Queue waiting notification scheduler stopped.")
