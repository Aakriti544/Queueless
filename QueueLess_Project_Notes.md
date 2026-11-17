# QueueLess — Project Understanding and Backend Planning

## 1. Project purpose

QueueLess is a remote queue-management system. A customer can join an organization's queue without waiting physically at the location, see their token and position, and receive a notification roughly 10 minutes before their turn is expected so they can travel to the organization and complete their service.

The backend will be built in **Python with Django from scratch**. Existing HTML templates were created by a friend and should be inspected and reused where appropriate rather than assumed to be complete or functional.

## 2. Main roles

### Customer / User
Expected capabilities:
- Register, log in, log out, and manage their profile.
- Find/select an organization, branch, and service.
- Join a queue and receive a token.
- View current token, queue position, estimated waiting time, and queue status.
- Receive and view notifications.
- Potentially leave/cancel a queue entry.

### Staff
Expected capabilities:
- Log in to a staff-specific dashboard.
- View queues assigned to their organization, branch, or counter.
- Call the next eligible customer.
- Mark a customer as served.
- Skip a customer who does not arrive and continue to the next eligible customer.
- View queue alerts and update queue progress.
- Potentially set or update estimated waiting times.

### Admin
The project intends to use Django's built-in admin panel for administrative management. Admin may manage user and staff accounts, organizations, branches, services, counters, queues, and monitor activity. The precise permissions and management scope still need confirmation.

**Important distinction:** Django's built-in admin account is not the same as a regular customer or staff account. Staff should have their own application dashboard and should not automatically receive full Django admin access.

## 3. Core queue workflow (proposed; confirm with project owner)

1. A customer signs in.
2. They choose an organization/branch and the service they need.
3. The system creates a queue entry and assigns a token.
4. The customer can see their token, number of people ahead, and estimated wait.
5. Staff call the next eligible entry from the queue.
6. If the customer responds/arrives, staff mark the entry as served when work is complete.
7. If the customer does not arrive, staff skip that entry and call the next eligible entry.
8. The system updates queue positions/statuses and sends notifications when appropriate.
9. A customer who is served, skipped, or cancels should no longer be treated as an ordinary waiting entry.

## 4. Notification requirement

The stated requirement is to notify a customer **at least approximately 10 minutes before their turn** so they have time to reach the organization.

The exact implementation is not decided yet. It depends on how the system estimates when a turn will occur, whether staff provide wait-time estimates, whether notifications are email/SMS/in-app/push, and whether the project must work when a user is not logged in. These details must be decided before implementing the notification system.

## 5. Existing HTML templates

The following templates were reported as already available:

- `admindashboard.html`
- `cd.html`
- `joinqueue.html`
- `queuestatus.html`
- `notifications.html`
- `profile.html`
- `login.html`
- `staffcallnext.html`
- `staffdashboard.html`
- `staffprofile.html`
- `staffalerts.html`
- `staffqueue.html`

The file `cd.html` was supplied for inspection. It is a customer dashboard template titled **“QueueLess - Customer Dashboard.”** Its visible interface includes:
- Sidebar links for Dashboard, Join Queue, My Queue, Notifications, Profile, and Logout.
- A welcome panel and Join a Queue button.
- Overview cards for Current Token, Queue Position, and Estimated Wait.
- A Current Queue panel with service, status, token, position, and wait.
- A Recent Notifications panel.

The template uses Django's `{% load static %}` tag and a static logo at `static/logo/logo.png`. Its JavaScript expects these URL paths:
- `/login.html/`
- `/cd.html/`
- `/api/user/logout/`
- `/joinqueue.html/`
- `/queuestatus.html/`
- `/notifications.html/`
- `/profile.html/`
- `/api/user/my-queue/`

The dashboard fetches `/api/user/my-queue/` and expects JSON with fields such as `success`, `has_queue`, and `queue`; the queue object may contain `token`, `service`, `position`, `estimated_wait` or `estimatedWait`, and `status`. It refreshes this data every five seconds. These are expectations in the existing frontend, **not proof that matching backend endpoints already exist**.

The logout request is a POST and includes a CSRF token. The frontend redirects to the login page after the request. The current dashboard includes placeholder content and assumes a bank branch name; these may need to become dynamic data.

## 6. Backend design considerations (not yet finalized)

Potential Django components:
- Authentication and role/permission handling.
- Organization and branch records.
- Services and counters.
- Queue/session records and individual queue entries.
- Queue token generation and ordering.
- Staff actions: call next, serve, skip.
- Customer queue-status endpoint or Django-rendered view.
- Notifications and a mechanism to trigger them.
- Admin registration and permissions.

Possible queue-entry states to consider: `waiting`, `called`, `serving`, `served`, `skipped`, and `cancelled`. These are suggestions, not final decisions.

The system should enforce queue rules on the server, not rely on browser JavaScript. For example, two staff actions must not accidentally call the same customer, and skipped/served entries should not be called again. Token uniqueness and queue ordering need a defined scope (for example, per branch, service, counter, and day).

## 7. Questions to settle before coding

1. **Organizations:** Can users choose multiple organizations (banks, hospitals, government offices, etc.)? Can each organization have multiple branches?
2. **Services and counters:** Does a branch have multiple services and counters? Can a staff member operate one counter or several?
3. **Joining:** Can users join remotely from anywhere? Should joining require a location check or arrival confirmation?
4. **Multiple queues:** Can one user hold more than one active queue entry at a time?
5. **Token format:** Should tokens be numeric (e.g. `15`) or prefixed (e.g. `A015`)? Should numbering reset daily and separately per service/counter?
6. **Calling and skipping:** How long does staff wait before skipping a customer? Can staff recall a skipped customer, or is skipping final?
7. **Service completion:** Does staff explicitly mark a called customer as served/completed?
8. **Estimated waiting time:** Is it entered by staff, calculated from queue position and average service duration, or both?
9. **Ten-minute notification:** What notification channels are required: in-app, email, SMS, push, or another method? Is email/SMS delivery expected for the first version?
10. **Staff accounts:** Does an admin create staff accounts and assign them to an organization/branch, or can staff register themselves?
11. **Admin scope:** Is Django admin sufficient, or is a custom admin dashboard also expected?
12. **Project constraints:** Which Django version/database are being used? Is the existing project already configured with models, apps, authentication, or migrations?
13. **Templates:** Are the other templates ready to connect, and should URLs preserve the paths the templates currently expect?

## 8. Suggested implementation order

1. Inspect the extracted Django project and settings.
2. Map all templates, forms, JavaScript requests, and URL paths.
3. Agree on the organization/branch/service/counter and queue rules.
4. Design models and relationships.
5. Implement authentication and role-based access.
6. Implement customer queue creation and status.
7. Implement staff queue actions, including safe call-next and skip behavior.
8. Implement notifications and the 10-minute alert logic.
9. Connect all templates, test permissions and edge cases, and use Django admin for administration.

## 9. Working principle

Build and understand the backend in small stages. Do not assume that a template's labels, URLs, or JavaScript behavior are already implemented on the server. Inspect the existing project and agree on the rules before writing the corresponding Django models, views, and endpoints.
# Customer wait estimate configuration

The customer-facing wait estimate uses one shared calculation in `user/services/queue.py` (`calculate_customer_wait_time`). Its baseline is configured by `CUSTOMER_WAIT_ESTIMATE_MINUTES` in `Queueless/settings.py`, currently `10`. Change that setting to tune the estimate; queue position, active staff workloads, and recorded service durations also affect the result.
