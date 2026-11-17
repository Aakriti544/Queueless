from django.db import transaction
from math import ceil

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from user.models import Queue, QueueTokenCounter, User


ACTIVE_QUEUE_STATUSES = ("waiting", "called", "serving")


def join_shared_queue(customer, service):
    """Create a tokenized organization-wide queue entry under database locks."""
    queue_date = timezone.localdate()
    with transaction.atomic():
        locked_customer = User.objects.select_for_update().get(
            pk=customer.pk, role="customer", is_active=True
        )
        existing = (
            Queue.objects.select_related("user")
            .filter(active_customer=locked_customer)
            .order_by("-created_at", "-pk")
            .first()
        )
        if existing:
            return existing, False

        # A singleton counter row is created by the schema migration. Locking it
        # serializes the first token assignment for each local calendar date.
        counter = QueueTokenCounter.objects.select_for_update().get(name="main")
        if counter.queue_date != queue_date:
            counter.queue_date = queue_date
            counter.last_token_number = 0
        counter.last_token_number += 1
        counter.save(update_fields=["queue_date", "last_token_number", "updated_at"])

        entry = Queue.objects.create(
            user=locked_customer,
            active_customer=locked_customer,
            service=service,
            queue_date=queue_date,
            token_number=counter.last_token_number,
            status="waiting",
        )
        return entry, True


def get_active_queue(customer):
    return (
        Queue.objects.filter(active_customer=customer)
        .select_related("user", "called_by")
        .order_by("-created_at", "-pk")
        .first()
    )


def queue_position(entry, now=None):
    """Return position and centrally estimated wait across the shared queue."""
    if entry.status != "waiting":
        return 0, 0, 0
    earlier = Q(created_at__lt=entry.created_at) | Q(
        created_at=entry.created_at, pk__lt=entry.pk
    )
    position = Queue.objects.filter(status="waiting").filter(
        earlier | Q(pk=entry.pk)
    ).count()
    people_ahead = max(0, position - 1)
    return position, people_ahead, calculate_customer_wait_time(
        entry, people_ahead, now=now
    )


def calculate_customer_wait_time(entry, people_ahead=None, now=None):
    """Estimate minutes until this waiting customer's service starts.

    Active staff workload is based on remaining assigned service time. Earlier
    waiting customers add their expected duration to the least-loaded staff.
    The estimate excludes the current customer's own service duration.
    """
    if entry.status != "waiting":
        return 0

    baseline = max(0, int(settings.CUSTOMER_WAIT_ESTIMATE_MINUTES))
    earlier = Q(created_at__lt=entry.created_at) | Q(
        created_at=entry.created_at, pk__lt=entry.pk
    )
    earlier_entries = list(
        Queue.objects.filter(status="waiting")
        .filter(earlier)
        .order_by("created_at", "pk")
        .values_list("service_duration_minutes", flat=True)
    )
    if people_ahead is not None:
        earlier_entries = earlier_entries[: max(0, people_ahead)]

    now = now or timezone.now()
    staff_ids = list(
        User.objects.filter(role="staff", is_active=True).values_list("pk", flat=True)
    )
    if not staff_ids:
        staff_ids = [None]
    workloads = {staff_id: 0 for staff_id in staff_ids}
    if staff_ids == [None]:
        # With no active staff account, retain the configured fallback estimate
        # instead of claiming an immediate turn from a nonexistent idle server.
        workloads[None] = baseline

    active_entries = Queue.objects.filter(status__in=("called", "serving")).select_related(
        "called_by"
    ).order_by("created_at", "pk")
    unassigned_work = []
    for active in active_entries:
        if active.service_duration_minutes:
            if active.status == "serving":
                elapsed = max(0, (now - active.updated_at).total_seconds() / 60)
                remaining = max(0, active.service_duration_minutes - elapsed)
            else:
                # A called entry has not started its assigned service yet.
                remaining = active.service_duration_minutes
        elif active.status == "serving":
            remaining = baseline
        else:
            remaining = baseline
        if active.called_by_id in workloads:
            workloads[active.called_by_id] += remaining
        else:
            unassigned_work.append(remaining)

    for workload in unassigned_work:
        least_loaded = min(workloads, key=workloads.get)
        workloads[least_loaded] += workload

    for expected_duration in earlier_entries:
        expected_duration = max(0, int(expected_duration or baseline))
        least_loaded = min(workloads, key=workloads.get)
        workloads[least_loaded] += expected_duration

    return max(0, int(ceil(min(workloads.values()))))
