from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase

from checkouts.models import CheckOut

from .factories import make_asset, make_authenticated_client, make_employee

FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt_timezone.utc)


class OverdueReportBoundaryTests(TestCase):
    """Rule: a check-out due exactly now is not yet overdue (the report
    filters on due_at < now, a strict inequality)."""

    def setUp(self):
        self.client = make_authenticated_client()
        self.employee = make_employee()

    @mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
    def test_due_exactly_now_is_excluded_but_one_second_past_is_included(self, _mock_now):
        due_now_asset = make_asset(tag="DUE-NOW")
        CheckOut.objects.create(
            asset=due_now_asset, employee=self.employee, due_at=FROZEN_NOW
        )

        overdue_asset = make_asset(tag="OVERDUE-1")
        CheckOut.objects.create(
            asset=overdue_asset,
            employee=self.employee,
            due_at=FROZEN_NOW - timedelta(seconds=1),
        )

        resp = self.client.get("/api/v1/reports/overdue/")
        self.assertEqual(resp.status_code, 200)
        tags = [row["asset_tag"] for row in resp.data["results"]]
        self.assertIn("OVERDUE-1", tags)
        self.assertNotIn("DUE-NOW", tags)

    @mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
    def test_most_overdue_first(self, _mock_now):
        asset_a = make_asset(tag="OD-A")
        asset_b = make_asset(tag="OD-B")
        CheckOut.objects.create(
            asset=asset_a, employee=self.employee,
            due_at=FROZEN_NOW - timedelta(days=1),
        )
        CheckOut.objects.create(
            asset=asset_b, employee=self.employee,
            due_at=FROZEN_NOW - timedelta(days=5),
        )

        resp = self.client.get("/api/v1/reports/overdue/")
        tags = [row["asset_tag"] for row in resp.data["results"]]
        self.assertEqual(tags, ["OD-B", "OD-A"])
