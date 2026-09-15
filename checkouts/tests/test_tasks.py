from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from checkouts.models import CheckOut, OverdueNotice
from checkouts.tasks import flag_overdue_checkouts

from .factories import make_asset, make_employee


class FlagOverdueCheckoutsTaskTests(TestCase):
    def setUp(self):
        self.employee = make_employee(code="TASK-1")
        self.overdue_checkout = CheckOut.objects.create(
            asset=make_asset(tag="TASK-A"),
            employee=self.employee,
            due_at=timezone.now() - timedelta(days=1),
        )
        self.not_due_yet_checkout = CheckOut.objects.create(
            asset=make_asset(tag="TASK-B"),
            employee=self.employee,
            due_at=timezone.now() + timedelta(days=1),
        )

    def test_creates_one_notice_for_the_overdue_checkout(self):
        flag_overdue_checkouts()
        self.assertEqual(OverdueNotice.objects.count(), 1)
        self.assertEqual(
            OverdueNotice.objects.first().checkout_id, self.overdue_checkout.id
        )

    def test_idempotent_running_twice_yields_one_notice(self):
        flag_overdue_checkouts()
        flag_overdue_checkouts()
        self.assertEqual(
            OverdueNotice.objects.filter(checkout=self.overdue_checkout).count(), 1
        )

    def test_does_not_notify_a_checkout_not_yet_due(self):
        flag_overdue_checkouts()
        self.assertFalse(
            OverdueNotice.objects.filter(checkout=self.not_due_yet_checkout).exists()
        )
