from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from checkouts.models import Asset, CheckOut

from .factories import make_asset, make_authenticated_client, make_employee


class CheckOutCreateRuleTests(TestCase):
    def setUp(self):
        self.client = make_authenticated_client()
        self.asset = make_asset()
        self.employee = make_employee()

    def _due_at(self, days=1):
        return (timezone.now() + timedelta(days=days)).isoformat()

    def test_checkout_success_sets_row_and_asset_status(self):
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, Asset.Status.CHECKED_OUT)
        self.assertEqual(CheckOut.objects.count(), 1)

    def test_rule1_unavailable_asset_returns_409(self):
        self.asset.status = Asset.Status.MAINTENANCE
        self.asset.save()
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_rule2_inactive_employee_returns_400(self):
        self.employee.is_active = False
        self.employee.save()
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rule3_fourth_open_checkout_returns_409(self):
        assets = [make_asset(tag=f"AST-{i}") for i in range(4)]
        for asset in assets[:3]:
            resp = self.client.post(
                "/api/v1/checkouts/",
                {
                    "asset_tag": asset.asset_tag,
                    "employee_code": self.employee.employee_code,
                    "due_at": self._due_at(),
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 201)

        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": assets[3].asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_rule4_due_at_in_past_returns_400(self):
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(days=-1),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rule4_due_at_too_far_ahead_returns_400(self):
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(days=31),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rule8_unknown_asset_tag_returns_404(self):
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": "DOES-NOT-EXIST",
                "employee_code": self.employee.employee_code,
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_rule8_unknown_employee_code_returns_404(self):
        resp = self.client.post(
            "/api/v1/checkouts/",
            {
                "asset_tag": self.asset.asset_tag,
                "employee_code": "DOES-NOT-EXIST",
                "due_at": self._due_at(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


class CheckOutReturnRuleTests(TestCase):
    def setUp(self):
        self.client = make_authenticated_client()
        self.asset = make_asset()
        self.employee = make_employee()
        self.asset.status = Asset.Status.CHECKED_OUT
        self.asset.save()
        self.checkout = CheckOut.objects.create(
            asset=self.asset,
            employee=self.employee,
            due_at=timezone.now() + timedelta(days=1),
        )

    def test_rule6_return_sets_returned_at_and_asset_available(self):
        resp = self.client.post(
            f"/api/v1/checkouts/{self.checkout.id}/return/",
            {"condition_note": "fine", "needs_maintenance": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.checkout.refresh_from_db()
        self.asset.refresh_from_db()
        self.assertIsNotNone(self.checkout.returned_at)
        self.assertEqual(self.asset.status, Asset.Status.AVAILABLE)

    def test_rule6_return_with_needs_maintenance_sets_asset_maintenance(self):
        resp = self.client.post(
            f"/api/v1/checkouts/{self.checkout.id}/return/",
            {"condition_note": "broken", "needs_maintenance": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, Asset.Status.MAINTENANCE)

    def test_rule6_double_return_returns_409(self):
        self.client.post(
            f"/api/v1/checkouts/{self.checkout.id}/return/",
            {"condition_note": "fine", "needs_maintenance": False},
            format="json",
        )
        resp = self.client.post(
            f"/api/v1/checkouts/{self.checkout.id}/return/",
            {"condition_note": "fine again", "needs_maintenance": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
