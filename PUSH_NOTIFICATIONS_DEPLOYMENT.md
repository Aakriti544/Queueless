# QueueLess push notifications deployment

## Production configuration

The customer push settings API (`GET /api/user/customer-push-subscriptions/`) returns a safe `configuration` diagnostic object. It reports missing or invalid setting names and whether the VAPID public and private keys match. It never returns key contents. The Notifications page uses this diagnostic to show which setting needs attention.

Set these environment variables in the production web service and every push worker:

- `WEB_PUSH_VAPID_PUBLIC_KEY`: URL safe base64 encoded uncompressed P-256 public key.
- `WEB_PUSH_VAPID_PRIVATE_KEY`: matching VAPID private key in the format supported by `pywebpush` (`py_vapid` encoded string or PEM file path).
- `WEB_PUSH_VAPID_SUBJECT`: a valid `mailto:` address or HTTPS contact URL.
- `WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY`: a Fernet key accepted by `cryptography.fernet.Fernet`.

Create each key pair once and keep it stable. Keep the same encryption key across deploys, worker processes, and instances because existing browser subscriptions are encrypted with it. Never generate keys during application startup, commit secrets, include them in logs, or place them in API responses. If the encryption key is lost or rotated, existing stored subscriptions cannot be decrypted; customers will need to disable and enable push again.

The VAPID public key is public and is returned to signed in customers so their browser can subscribe. Keep the private VAPID key and the Fernet encryption key in the hosting provider's secret environment configuration. The runtime dependencies are already listed in `requirements.txt` (`pywebpush` and `cryptography`). No new migration is required for this push setup; apply the existing user migrations as part of deployment (`python manage.py migrate`).

## Workers

Queue events create durable, deduplicated push delivery rows. A separate process must deliver them. Run the following commands as persistent background workers alongside the Django web service:

```text
python manage.py send_queue_pushes
python manage.py send_queue_wait_updates
```

On a platform that only supports scheduled jobs, schedule both with `--once` at a frequent interval (for example, once per minute):

```text
python manage.py send_queue_pushes --once
python manage.py send_queue_wait_updates --once
```

Use the same code release, database, and four Web Push settings for the workers as for the web service. The push worker retries temporary failures, and removes subscriptions rejected by the push service as expired or invalid. The wait update scheduler creates deduplicated queue position and wait notifications in the existing outbox; the push worker sends them.

## Browser smoke test

1. Serve QueueLess over HTTPS (localhost is also considered a secure context by browsers).
2. Sign in as a customer, open Notifications, and confirm the page reports configured status. If it does not, review the returned `configuration.missing_or_invalid` names without logging secret values.
3. Enable push and grant browser permission. The UI reports success only after the subscription API saves the subscription.
4. Keep `send_queue_pushes` running. Join a queue and confirm the queued “Queue joined” push is delivered. Have staff call the token and confirm the call update.
5. Move the browser to the background or close the tab and repeat with another queue event. Delivery depends on the browser and operating system allowing background push.
6. Disable push from Notifications and confirm that device subscription is removed.

The existing notification polling remains active as a fallback when push is unavailable or delayed.
