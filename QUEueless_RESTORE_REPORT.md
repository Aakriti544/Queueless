# QueueLess Restore Report

**Date:** 2026-11-20

## Restore point and method

The desired point was not a Git commit. `git log --all` showed five commits, with `2e4c161` (`Replace SMTP email delivery with Brevo API`) at `HEAD`; `git branch -avv` showed only `main`/`origin/main`, no tags were present, and the reflog contained only the clone. The API fetch/loading improvements and later staff-contact/migration work were both uncommitted.

I reconstructed the requested working-tree state by separating the diffs: preserve the polling, cache/request-coalescing, incremental notification/history, and staff-queue query improvements; remove the staff phone/address and migration/audit changes; and restore migration-compatibility locking edits to the pre-audit version. This is a targeted reconstruction of the requested pre-contact state, not a checkout of a historical commit.

## Backup created before restoration

A complete copy of the project, including `.git`, hidden files, untracked files, and the existing `.venv`, was made outside the project folder:

`C:\Users\A PLUS\Desktop\LastQueueless\Queueless_pre_restore_backup_20261120_000227`

Robocopy reported **8,358 files copied, 0 failures** (99.02 MB). The backup contains `RESTORE_BACKUP_METADATA.txt` with the original HEAD, status, recent log, and copy result. The backup was not overwritten or deleted.

## Files restored or removed

Restored contact/schema/audit-era edits to `HEAD` where those files contained no independent performance changes:

- `user/models.py`, `user/admin.py`, `user/views/auth.py`, `user/tests.py`
- `user/services/notifications.py`, `user/services/push_delivery.py`, `user/services/wait_notifications.py`
- `template/staffprofile.html`

Restored the `call_next_api()` locking clause/comment in `user/views/staff.py` to the pre-compatibility-audit version, while retaining its staff queue query optimizations. Removed the phone/address form handling from `user/views/admin.py` and `template/admindashboard.html`, preserving their API projection/caching changes.

Removed these migration/audit-only files, which were untracked and absent at `HEAD`:

- `user/migrations/0017_queue_active_customer_unique.py`
- `user/migrations/0018_notification_query_indexes.py`
- `user/migrations/0019_staff_contact_details.py`
- `user/test_migration_0017_safety.py`
- `scripts/inspect_queue_migration_0017.py`
- `QUEueless_MIGRATION_AUDIT_REPORT.md`

The database configuration and committed migrations `0001`–`0016` were left intact. No production connection was opened, and no database migration or record operation was run.

## API fetch/loading work preserved

The following local performance changes remain in the working tree:

- `static/js/api.js` polling helper with visibility-aware timer and in-flight request guard.
- Admin Dashboard customer/staff request reuse, cached lists/history, and queue polling while the relevant view is active.
- Customer/staff page polling and queue refresh changes in the existing templates.
- Incremental notification fetching and bounded history pagination in `user/views/queue.py`.
- Staff queue query/result reuse and aggregation in `user/views/staff.py`.
- Customer refresh regression tests in `user/test_customer_refresh.py`.

No fetch calls were removed as part of this restore. The Admin Staff form is back to Staff Name, Email, and Password only. `rg` confirmed no `staffPhone`, `staffAddress`, or `validate_staff_phone` references remain in application templates/code.

## Verification

All Django commands below used a temporary settings module with an in-memory SQLite test database; the configured TiDB database was not used.

| Check | Result |
| --- | --- |
| `python manage.py check` | Passed: no issues. |
| `python manage.py makemigrations --check --dry-run` | Passed: no changes detected; model state matches migrations `0001`–`0016`. |
| Relevant customer refresh, Admin/Customer/FlexiblePassword, email, push, and wait-notification tests | **56 passed.** |
| Full `user` suite | **83 tests: 80 passed, 1 failed, 2 errored.** |
| JavaScript syntax | Passed for 9 files/inline script blocks from the changed polling-enabled pages. |
| Python compilation | Passed for the project Python modules. |
| `git diff --check` | Passed. |

The full-suite failure `test_database_constraint_prevents_two_active_entries` expects the unique database constraint that migration 0017 provided. Removing migrations 0017–0019 as requested leaves the baseline `active_customer` ForeignKey without that database-level uniqueness, so this test fails. I preserved the test rather than removing or weakening it. The two errors were `test_concurrent_customers_get_distinct_daily_tokens` and `test_concurrent_same_customer_join_reuses_one_active_entry`; both exercise threaded concurrency on SQLite and hit SQLite locking/barrier limitations. These are not TiDB results.

Template rendering/navigation tests in the focused Admin/Customer tests passed. No contact-field references remain in those screens, and existing staff creation, authentication, and management tests in the focused set passed.

## Remaining issue

The restored pre-0017 schema does not enforce one active queue entry per customer at the database level. This is why the preserved constraint test fails. The earlier application-level join behavior remains, but database-level protection requires migration 0017 or another reviewed solution. No migration was applied here.

## Final status

The staff phone/address feature and migration/audit-only files have been removed from the local source tree, and the API fetch/loading optimizations remain. The complete suite is not fully green for the reasons above. No production database or records were changed.
