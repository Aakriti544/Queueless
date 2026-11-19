window.queueLessReadJson = async function (response) {
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const path = (() => {
    try { return new URL(response.url, window.location.href).pathname; }
    catch { return "the requested endpoint"; }
  })();

  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch (error) {
      throw new Error(`The server returned invalid JSON for ${path} (HTTP ${response.status}).`);
    }
  }

  if (response.redirected && /login\.html\/?$/i.test(path)) {
    window.location.replace("/login.html/");
    throw new Error("Your session has expired. Please sign in again.");
  }

  const kind = contentType.includes("text/html") ? "an HTML page" : "a non-JSON response";
  throw new Error(`Expected JSON from ${path}, but the server returned ${kind} (HTTP ${response.status}). Check the server response and application logs.`);
};

window.queueLessStartPolling = function (callback, intervalMs) {
  let timer = null;
  let requestActive = false;
  let lastStartedAt = 0;
  const stop = () => {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  };
  const refresh = () => {
    if (document.visibilityState !== "visible" || requestActive) return;
    const now = Date.now();
    if (now - lastStartedAt < 750) return;
    requestActive = true;
    lastStartedAt = now;
    Promise.resolve().then(callback).catch((error) => {
      console.error("QueueLess background refresh failed:", error);
    }).finally(() => { requestActive = false; });
  };
  const start = () => {
    stop();
    if (document.visibilityState === "visible") timer = window.setInterval(refresh, intervalMs);
  };
  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") {
      start();
      refresh();
    } else {
      stop();
    }
  };
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("focus", refresh);
  start();
  return {
    refresh,
    stop: () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", refresh);
    },
  };
};
