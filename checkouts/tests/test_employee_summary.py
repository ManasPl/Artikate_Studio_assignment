from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from checkouts.models import CheckOut

from .factories import make_asset, make_authenticated_client, make_employee


class EmployeeSummaryTests(TestCase):
    def setUp(self):
        self.client = make_authenticated_client()
        self.employee = make_employee(code="SUM-1")
        self.now = timezone.now()

    def test_four_numbers_against_controlled_data(self):
        # Held, not yet due -> counts toward held but not overdue.
        CheckOut.objects.create(
            asset=make_asset(tag="SUM-A"),
            employee=self.employee,
            due_at=self.now + timedelta(days=2),
        )
        # Held, overdue -> counts toward held and overdue.
        overdue = CheckOut.objects.create(
            asset=make_asset(tag="SUM-B"),
            employee=self.employee,
            due_at=self.now - timedelta(days=1),
        )
        # Returned: held 4 days.
        returned_a = CheckOut.objects.create(
            asset=make_asset(tag="SUM-C"),
            employee=self.employee,
            due_at=self.now + timedelta(days=10),
        )
        CheckOut.objects.filter(pk=returned_a.pk).update(
            checked_out_at=self.now - timedelta(days=10),
            returned_at=self.now - timedelta(days=6),
        )
        # Returned: held 2 days.
        returned_b = CheckOut.objects.create(
            asset=make_asset(tag="SUM-D"),
            employee=self.employee,
            due_at=self.now + timedelta(days=10),
        )
        CheckOut.objects.filter(pk=returned_b.pk).update(
            checked_out_at=self.now - timedelta(days=5),
            returned_at=self.now - timedelta(days=3),
        )

        resp = self.client.get(f"/api/v1/employees/{self.employee.employee_code}/summary/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["lifetime_checkout_count"], 4)
        self.assertEqual(resp.data["currently_held"], 2)
        self.assertEqual(resp.data["currently_overdue"], 1)
        # Mean of 4 and 2 days = 3.0
        self.assertAlmostEqual(resp.data["mean_hold_duration_days"], 3.0, places=1)

    def test_unknown_employee_code_returns_404(self):
        resp = self.client.get("/api/v1/employees/DOES-NOT-EXIST/summary/")
        self.assertEqual(resp.status_code, 404)
