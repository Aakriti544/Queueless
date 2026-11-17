/* QueueLess Web Push worker. Payload text intentionally contains no customer details. */
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {};
  }

  const title =
    typeof payload.title === "string" && payload.title.length <= 80
      ? payload.title
      : "QueueLess update";
  const body =
    typeof payload.body === "string" && payload.body.length <= 160
      ? payload.body
      : "Your queue status changed. Open QueueLess to view details.";
  const url =
    typeof payload.url === "string" && payload.url.startsWith("/") && !payload.url.startsWith("//")
      ? payload.url
      : "/notifications.html/";

  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, {
        body,
        icon: "/static/logo/logo.png",
        badge: "/static/logo/logo.png",
        data: { url },
        renotify: false,
      }),
      clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) =>
        Promise.all(
          windows.map((client) =>
            client.postMessage({ type: "queueless-queue-update" }),
          ),
        ),
      ),
    ]),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/notifications.html/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const existing = windows.find((client) => client.url.startsWith(self.location.origin));
      if (existing) {
        return existing.focus().then(() => existing.navigate(url));
      }
      return clients.openWindow(url);
    }),
  );
});
