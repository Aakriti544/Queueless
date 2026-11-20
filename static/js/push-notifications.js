(function () {
  const apiUrl = "/api/user/customer-push-subscriptions/";
  const workerUrl = "/static/js/queueLessServiceWorker.js";
  const statusElement = document.getElementById("pushSettingsStatus");
  const button = document.getElementById("pushSettingsButton");
  if (!statusElement || !button) return;

  let serverState = null;
  let registration = null;
  let currentSubscription = null;

  function csrfToken() {
    const item = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith("csrftoken="));
    return item ? decodeURIComponent(item.slice("csrftoken=".length)) : "";
  }

  async function readJson(response) {
    if (window.queueLessReadJson) return window.queueLessReadJson(response);
    return response.json();
  }

  async function apiRequest(method, body) {
    const headers = { Accept: "application/json" };
    const options = { method, credentials: "same-origin", headers };
    if (body) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRFToken"] = csrfToken();
      options.body = JSON.stringify(body);
    }
    const response = await fetch(apiUrl, options);
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "Push settings could not be updated.");
    return data;
  }

  function supported() {
    return (
      window.isSecureContext &&
      "Notification" in window &&
      "serviceWorker" in navigator &&
      "PushManager" in window
    );
  }

  function decodeApplicationServerKey(value) {
    const padding = "=".repeat((4 - (value.length % 4)) % 4);
    const decoded = atob(value.replace(/-/g, "+").replace(/_/g, "/") + padding);
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  }

  function waitUntilActive(workerRegistration) {
    if (workerRegistration.active) return Promise.resolve(workerRegistration);
    const worker = workerRegistration.installing || workerRegistration.waiting;
    if (!worker) return Promise.reject(new Error("The notification worker did not start."));
    return new Promise((resolve, reject) => {
      const checkState = () => {
        if (worker.state === "activated") {
          worker.removeEventListener("statechange", checkState);
          resolve(workerRegistration);
        } else if (worker.state === "redundant") {
          worker.removeEventListener("statechange", checkState);
          reject(new Error("The notification worker could not be activated."));
        }
      };
      worker.addEventListener("statechange", checkState);
      checkState();
    });
  }

  async function findLocalSubscription() {
    registration = await navigator.serviceWorker.getRegistration("/static/js/");
    if (!registration) return null;
    return registration.pushManager.getSubscription();
  }

  function render(message, canDisable = false) {
    statusElement.textContent = message;
    button.disabled = !serverState?.configured || (!canDisable && Notification.permission === "denied");
    button.textContent = currentSubscription
      ? "Disable push on this device"
      : "Enable push notifications";
    if (currentSubscription && serverState?.configured) button.disabled = false;
  }

  async function refresh() {
    button.disabled = true;
    if (!supported()) {
      statusElement.textContent = window.isSecureContext
        ? "This browser does not support Web Push notifications."
        : "Web Push requires HTTPS or localhost. Open QueueLess over a secure connection to enable notifications.";
      return;
    }
    try {
      serverState = await apiRequest("GET");
      currentSubscription = await findLocalSubscription();
      if (!serverState.configured) {
        statusElement.textContent = "Push notifications are not configured on this QueueLess server.";
        button.disabled = true;
      } else if (Notification.permission === "denied" && !currentSubscription) {
        statusElement.textContent = "Notifications are blocked in browser settings. Allow them there to enable push.";
        button.disabled = true;
      } else if (currentSubscription) {
        statusElement.textContent = "Push notifications are enabled.";
        render(statusElement.textContent, true);
      } else if (Notification.permission === "granted") {
        statusElement.textContent = "Push is allowed by your browser but is not enabled on this device.";
        render(statusElement.textContent, true);
      } else {
        statusElement.textContent = "Enable push to receive concise queue updates when QueueLess is in the background.";
        render(statusElement.textContent);
      }
    } catch (error) {
      statusElement.textContent = error.message;
      button.disabled = true;
    }
  }

  async function enablePush() {
    statusElement.textContent = "Enabling push notifications…";
    button.disabled = true;
    try {
      if (Notification.permission === "denied") {
        statusElement.textContent = "Notifications are blocked in browser settings. Allow them there, then try again.";
        return;
      }
      const permission = Notification.permission === "granted"
        ? "granted"
        : await Notification.requestPermission();
      if (permission !== "granted") {
        statusElement.textContent = permission === "denied"
          ? "Notifications were denied. Change this site's permission in browser settings to enable push."
          : "Push notifications were not enabled.";
        button.disabled = permission === "denied";
        return;
      }
      registration = await navigator.serviceWorker.register(workerUrl);
      await waitUntilActive(registration);
      currentSubscription = await registration.pushManager.getSubscription();
      if (!currentSubscription) {
        currentSubscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: decodeApplicationServerKey(serverState.vapid_public_key),
        });
      }
      await apiRequest("POST", { subscription: currentSubscription.toJSON() });
      statusElement.textContent = "Push notifications enabled successfully!";
      render(statusElement.textContent, true);
    } catch (error) {
      statusElement.textContent = error.message || "Push notifications could not be enabled.";
      // Leave the action as Enable so the next click can retry saving an
      // existing browser subscription instead of unexpectedly unsubscribing it.
      currentSubscription = null;
      render(statusElement.textContent);
    }
  }

  async function disablePush() {
    try {
      if (currentSubscription) {
        await apiRequest("DELETE", { subscription: currentSubscription.toJSON() });
        await currentSubscription.unsubscribe();
      }
      currentSubscription = null;
      statusElement.textContent = "Push notifications are disabled.";
      render(statusElement.textContent);
    } catch (error) {
      statusElement.textContent = error.message;
      button.disabled = false;
    }
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    if (currentSubscription) await disablePush();
    else await enablePush();
  });

  document.addEventListener("DOMContentLoaded", refresh);
  if (document.readyState !== "loading") refresh();
})();
