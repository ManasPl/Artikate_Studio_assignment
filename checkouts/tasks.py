from celery import shared_task
from django.utils import timezone

from .models import CheckOut, OverdueNotice


@shared_task
def flag_overdue_checkouts():
    """Creates one OverdueNotice per open, overdue check-out, dated today.

    Idempotent: the (checkout, notice_date) unique constraint on
    OverdueNotice means running this five times in one day still yields
    exactly one notice per check-out for that day. We pre-filter out
    checkouts already notified today (two queries, no per-row loop) and
    still pass ignore_conflicts=True on the bulk insert as a backstop
    against a race between two overlapping task runs.
    """
    today = timezone.localdate()
    overdue_checkout_ids = list(
        CheckOut.objects.filter(
            returned_at__isnull=True,
            due_at__lt=timezone.now(),
        ).values_list("id", flat=True)
    )

    already_notified_ids = set(
        OverdueNotice.objects.filter(
            notice_date=today, checkout_id__in=overdue_checkout_ids
        ).values_list("checkout_id", flat=True)
    )

    notices = [
        OverdueNotice(checkout_id=checkout_id, notice_date=today)
        for checkout_id in overdue_checkout_ids
        if checkout_id not in already_notified_ids
    ]
    OverdueNotice.objects.bulk_create(notices, ignore_conflicts=True)
    return f"flagged {len(notices)} new overdue notice(s) for {today}"
