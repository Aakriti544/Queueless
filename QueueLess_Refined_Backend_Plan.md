# QueueLess — Refined Backend Plan
**Updated:** 29 October 2026

## 1. Project goal

QueueLess is a Django web application that lets customers join a service queue remotely, track their token and queue position, and receive notifications as their turn approaches. Staff manage the live queue from a staff dashboard. Administrators manage staff accounts and system data through Django's built-in admin site.

This plan is based on the decisions confirmed by the project owner. It does not assume that the existing HTML templates already have working Django views, URLs, models, or APIs.

## 2. Confirmed decisions

| Area | Decision | Backend consequence |
|---|---|---|
| Organization | Single organization | No multi-tenant organization model is needed for the first version. |
| Joining | Remote | Authenticated customers can join online. |
| Active queues per customer | One | Enforce one active queue entry per customer. |
| Token numbering | Daily | Token numbers reset daily for the queue. |
| Notifications | In-app and email | Persist notification records and send email messages. |
| Staff account creation | Admin creates staff | No public staff self-registration. |
| Estimated wait | Staff-managed | Staff can set/update the estimated wait; backend exposes it to customers. |
| Absent customers | Grace period | A called customer remains in a grace state/window before staff can skip them. |
| Database | SQLite | Use Django's default SQLite setup for development and the initial project. |
| Extra requirements | None provided | Do not add unrelated features without asking. |

## 3. Roles and permissions

### Customer
- Register, log in, log out, and update permitted profile fields.
- View available services.
- Join a queue remotely.
- View their token, current position, estimated waiting time, and queue status.
- View and mark in-app notifications as read.
- Receive relevant email notifications.
- Leave/cancel their queue entry if the project rules allow cancellation (confirm before implementing).

### Staff
- Log in to the staff dashboard.
- View the current queue and customer details needed to serve the next person.
- Set/update the estimated waiting time.
- Call the next eligible customer.
- Start and complete service.
- Mark a called customer absent only after the grace period expires.
- See queue-related alerts and the action history needed for troubleshooting.

Staff must not automatically have Django superuser access.

### Administrator
- Use Django's built-in `/admin/` site.
- Create staff accounts and assign staff permissions.
- Manage service types and other basic project data.
- Review users, queue entries, and notifications when necessary.

Use Django's built-in authentication and permission system where possible. Protect customer and staff views on the server; hiding links in HTML is not access control.

## 4. Proposed data models

Keep the initial schema small. Exact fields can be adjusted after inspecting the whole project.

### `User`
Start with Django's built-in `User` unless the existing project already requires a custom user model. Decide this before running the first migration. Use Django groups/permissions or a carefully validated role field to distinguish customer and staff access.

### `Service`
Represents a service offered by the single organization, for example account services or document services.
- `name`
- `description` (optional)
- `is_active`
- creation/update timestamps as needed

### `QueueEntry`
One customer's queue visit.
- `customer` — foreign key to user
- `service` — foreign key to service
- `queue_date` — date used for daily numbering
- `token_number` — integer
- `status` — controlled choices
- `joined_at`
- `called_at` (nullable)
- `service_started_at` (nullable)
- `completed_at` (nullable)
- `grace_deadline` (nullable)
- `estimated_wait_minutes` or a reference to the current staff-set estimate
- optional `skipped_at` / `cancelled_at` timestamps if useful

Suggested statuses:
- `waiting`
- `called`
- `serving`
- `served`
- `skipped`
- `cancelled`

Only `waiting`, `called`, and `serving` count as active for the one-active-queue rule. A called entry stays active during the grace period. Served, skipped, and cancelled entries are terminal states.

### `Notification`
- `recipient`
- `title`
- `message`
- `notification_type`
- optional `queue_entry`
- `is_read`
- `created_at`
- optional `email_sent_at` / delivery status

Store the in-app notification before attempting email delivery so a temporary email failure does not erase the notification.

### Optional `QueueAction` / audit log
A small audit model can record which staff member called, started, completed, or skipped a queue entry and when. This helps explain queue changes, but can be postponed until the core workflow works.

## 5. Queue rules that must be enforced on the backend

1. **One active queue per customer.** Check this in the server-side join operation. A database constraint may be added if it can express the rule reliably for the chosen schema; application checks alone can race under concurrent requests.
2. **Daily token numbering.** Number tokens for the current local date and the chosen queue scope. Since the project has one organization, decide whether numbering resets per service or across all services. Recommended default: reset per service each day, unless the UI or project requirements imply one shared line.
3. **Concurrency.** Two customers joining at nearly the same time must not receive the same token. Use a database transaction and a reliable uniqueness strategy for `(service, queue_date, token_number)`. SQLite is fine for a college prototype, but has limits for high-concurrency production use.
4. **Queue position.** Calculate from the current eligible waiting entries rather than storing a position that becomes stale whenever someone is called, served, skipped, or cancels.
5. **Call next.** Select the first eligible waiting entry and atomically change it to `called`; do not allow duplicate calls for the same entry.
6. **Grace period.** When staff calls a customer, save `called_at` and `grace_deadline`. Staff cannot skip them before the deadline. The exact grace duration is still undecided and should be configurable.
7. **Skip absent customer.** After the grace deadline, staff can change `called` to `skipped`. The skipped entry no longer occupies a waiting position.
8. **Serving and completion.** `called` → `serving` → `served`. Validate each transition; do not let a completed entry return to the waiting queue.
9. **Staff-set estimate.** Validate that the estimated wait is a non-negative number. Show it to the customer and use it as the basis for the planned reminder. Staff should be able to update it when conditions change.
10. **Authorization.** Customers can only see their own queue entries and notifications. Staff queue actions require staff permissions.
11. **Email reliability.** Do not send email directly in a way that blocks every request if the mail server is slow. Start with Django's email backend and a clear failure log; add a background worker/scheduler if needed.
12. **Timezone.** Configure the project's local timezone explicitly and use timezone-aware datetimes. Daily token reset must follow the organization's local date, not an accidental UTC date.

## 6. Notifications

### In-app notifications
Create a database record for important events, such as:
- Queue entry created and token assigned.
- Estimated turn is approaching.
- Staff has called the customer.
- Queue entry was skipped or cancelled, where relevant.

### Email notifications
Send matching email updates for the key events. For the “about 10 minutes before the turn” reminder:
- Use the current staff-provided estimated wait and queue position as an estimate, not a guarantee.
- Record whether the reminder has already been sent to prevent duplicates.
- Re-evaluate reminders when staff changes the estimate or queue order changes.
- Use a scheduled background process for dependable delivery. A request-time timer or a page's JavaScript timer is not sufficient because it stops working when the browser is closed or nobody visits the site.
- Always send an immediate “you have been called” notification as a separate safety net.

For local development, use Django's console email backend first; switch to SMTP credentials in environment variables once the app flow is tested. Never commit real email passwords.

## 7. Existing templates and integration

Templates reported so far:
- `admindashboard.html`
- `cd.html`
- `notifications.html`
- `staffcallnext.html`
- `profile.html`
- `staffdashboard.html`
- `joinqueue.html`
- `queuestatus.html`
- `staffprofile.html`
- `login.html`
- `staffalerts.html`
- `staffqueue.html`

The inspected `cd.html` template includes:
- Navigation to dashboard, join queue, queue status, notifications, profile, and logout.
- Cards for current token, queue position, and estimated wait.
- A current-queue panel and recent notifications.
- JavaScript polling `/api/user/my-queue/` every five seconds.
- An expected response shape with `success`, `has_queue`, and `queue` fields, including token, service, position, estimated wait, and status.
- A POST request to `/api/user/logout/`.

These are frontend expectations only; do not assume those endpoints exist until the project files are inspected. Replace hard-coded sample content such as “QueueLess Bank — Main Branch” with real project context where appropriate. Prefer Django `{% url %}` template tags over hard-coded internal links once URL names are established.

## 8. Suggested endpoint/view map

Use regular Django views and forms for server-rendered templates unless the existing JavaScript genuinely requires JSON endpoints. Keep URL names consistent.

### Authentication
- `register` — customer registration
- `login` / `logout`
- `profile`

### Customer queue
- `join_queue` — GET form and POST queue creation
- `my_queue` — current active entry and position
- `queue_status` — queue details/status page
- `cancel_queue` — only if cancellation is approved as a project rule

### Notifications
- `notifications` — list the logged-in customer's notifications
- `mark_notification_read`

### Staff
- `staff_dashboard`
- `staff_queue`
- `update_estimated_wait`
- `call_next`
- `start_service`
- `complete_service`
- `skip_customer`

### Admin
- Django built-in `/admin/`; no custom admin panel is required unless the existing template specifically needs one.

If `/api/user/my-queue/` is retained, return a documented JSON structure compatible with `cd.html`, and require authentication.

## 9. Implementation sequence

1. **Inspect the complete extracted project**: `manage.py`, settings, root and app `urls.py`, installed apps, models, views, forms, migrations, templates, static files, and `.env.example`. Keep working code where it is correct; do not rebuild blindly.
2. **Confirm the Django app structure and custom-user decision** before migrations.
3. **Configure settings**: SQLite, templates/static files, timezone, login redirects, email backend and environment variables.
4. **Build authentication and permissions**: customer registration/login; admin-created staff accounts; protected staff routes.
5. **Create and migrate models**: Service, QueueEntry, Notification, plus optional audit log.
6. **Implement queue core**: join, token allocation, active-entry rule, position calculation, daily reset.
7. **Implement staff actions**: update estimate, call next, grace-period validation, start service, complete, skip.
8. **Implement in-app notifications**, then email delivery and scheduled reminders.
9. **Connect the existing templates** and replace placeholder data with context from views/endpoints.
10. **Test edge cases**: duplicate joins, simultaneous token creation, unauthorized access, daily reset, called-customer grace period, skipped customers leaving the waiting order, email failures, and notification duplicates.

## 10. Acceptance tests

The backend is not ready until these are true:
- A logged-in customer can join remotely and receive a unique token.
- The same customer cannot have two active queue entries.
- Token numbers reset on the next local calendar day according to the chosen queue scope.
- Customers cannot read other customers' queue data or notifications.
- Staff cannot skip a called customer before the grace deadline.
- Calling, serving, completing, and skipping correctly update the queue and positions.
- Updating the estimate is reflected on the customer dashboard.
- In-app notifications persist, and email failures are handled without losing those records.
- Reminder jobs avoid sending the same reminder multiple times.
- Admin can create staff accounts; staff cannot access Django superuser-only functions.

## 11. Decisions still to confirm

These are not blockers for inspecting the project, but should be settled before implementing the related behavior:

1. **Token scope:** daily numbering per service (recommended) or one shared number sequence across all services?
2. **Grace period:** how long after a call before staff may skip the customer? Suggested starting default: 3 minutes, configurable.
3. **Ten-minute reminder:** send when the estimated wait first reaches about 10 minutes, or schedule it for the predicted turn time minus 10 minutes? Recommended: schedule at predicted turn minus 10 minutes, then re-evaluate when estimates/order change.
4. **Cancellation:** may customers cancel their queue entry themselves?
5. **Email scheduling:** a lightweight scheduled command for a local/college demo, or a persistent worker/scheduler? Start with a management command run periodically during development if a separate worker would be too much for the first milestone.

## 12. Development approach

Build and test one small layer at a time. For each step, explain:
- what the code does,
- why it belongs in that file,
- how Django calls it,
- how to test it,
- and what could go wrong.

Do not write all views and models at once before understanding the existing project structure.
