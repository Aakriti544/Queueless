from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CustomerQueueRefreshTests(unittest.TestCase):
    def test_customer_queue_pages_poll_once_per_three_minutes(self):
        dashboard = (PROJECT_ROOT / "template" / "customerDashboard.html").read_text(
            encoding="utf-8"
        )
        status = (PROJECT_ROOT / "template" / "queuestatus.html").read_text(
            encoding="utf-8"
        )

        self.assertEqual(dashboard.count("setInterval(loadCustomerQueue, 180000);"), 1)
        self.assertEqual(status.count("setInterval(loadQueueStatus, 180000);"), 1)
        self.assertEqual(status.count("setInterval(checkQueueNotifications, 10000);"), 1)
        self.assertNotIn("setInterval(loadCustomerQueue, 5000)", dashboard)
        self.assertNotIn("setInterval(loadQueueStatus, 5000)", status)

    def test_important_queue_events_trigger_refresh_without_waiting_for_interval(self):
        dashboard = (PROJECT_ROOT / "template" / "customerDashboard.html").read_text(
            encoding="utf-8"
        )
        status = (PROJECT_ROOT / "template" / "queuestatus.html").read_text(
            encoding="utf-8"
        )
        worker = (PROJECT_ROOT / "static" / "js" / "queueLessServiceWorker.js").read_text(
            encoding="utf-8"
        )

        for page, callback in (
            (dashboard, "loadCustomerQueue"),
            (status, "loadQueueStatus"),
        ):
            self.assertIn('"queueless-queue-update"', page)
            self.assertIn(f"window.addEventListener(\"focus\", {callback})", page)
        self.assertIn('postMessage({ type: "queueless-queue-update" })', worker)
        self.assertIn("loadCustomerQueue();", dashboard)  # new in-app notification
        self.assertIn("loadQueueStatus();", status)  # new in-app notification
