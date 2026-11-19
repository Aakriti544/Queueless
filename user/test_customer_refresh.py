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
        polling = (PROJECT_ROOT / "static" / "js" / "api.js").read_text(
            encoding="utf-8"
        )

        self.assertEqual(dashboard.count("queueLessStartPolling(loadCustomerQueue, 180000)"), 1)
        self.assertEqual(status.count("queueLessStartPolling(loadQueueStatus, 180000)"), 1)
        self.assertEqual(status.count("queueLessStartPolling(checkQueueNotifications, 10000)"), 1)
        self.assertNotIn("setInterval(loadCustomerQueue, 5000)", dashboard)
        self.assertNotIn("setInterval(loadQueueStatus, 5000)", status)
        self.assertIn('document.visibilityState === "visible"', polling)
        self.assertIn("window.clearInterval(timer)", polling)
        self.assertIn("requestActive", polling)

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

        self.assertIn('"queueless-queue-update"', dashboard)
        self.assertIn('"queueless-queue-update"', status)
        self.assertIn("customerQueuePoller?.refresh()", dashboard)
        self.assertIn("queueStatusPoller.refresh()", status)
        self.assertIn('postMessage({ type: "queueless-queue-update" })', worker)
        self.assertIn("loadCustomerQueue();", dashboard)  # new in-app notification
        self.assertIn("loadQueueStatus();", status)  # new in-app notification

    def test_other_polling_pages_use_visibility_aware_polling(self):
        for filename in (
            "admindashboard.html",
            "notifications.html",
            "staffdashboard.html",
            "staffqueue.html",
            "staffcallnext.html",
            "staffalerts.html",
        ):
            page = (PROJECT_ROOT / "template" / filename).read_text(encoding="utf-8")
            self.assertIn("queueLessStartPolling(", page, filename)
            self.assertNotIn("setInterval(", page, filename)
