import time

from django.core.management.base import BaseCommand, CommandError

from user.services.push_delivery import process_pending_queue_pushes


class Command(BaseCommand):
    help = "Deliver queued QueueLess Web Push notifications and retry temporary failures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process one batch and exit (suitable for a scheduled invocation).",
        )
        parser.add_argument("--poll-interval", type=int, default=5)
        parser.add_argument("--batch-size", type=int, default=25)

    def handle(self, *args, **options):
        interval = options["poll_interval"]
        batch_size = options["batch_size"]
        if interval < 1:
            raise CommandError("--poll-interval must be at least 1 second.")
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")

        self.stdout.write("Queue push worker started.")
        try:
            while True:
                result = process_pending_queue_pushes(batch_size=batch_size)
                if result["processed"]:
                    self.stdout.write(
                        "Processed {processed}: sent={sent}, retrying={retrying}, "
                        "failed={failed}, expired={expired}.".format(**result)
                    )
                if options["once"]:
                    break
                if not result["processed"]:
                    time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Queue push worker stopped.")
