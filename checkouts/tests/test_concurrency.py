import threading
from datetime import timedelta

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from checkouts.models import CheckOut

from .factories import make_asset, make_authenticated_client, make_employee


class ConcurrentCheckoutTests(TransactionTestCase):
    """Rule 7: two simultaneous check-out requests for the same asset must
    result in exactly one 201 and one 409 — enforced by SELECT ... FOR
    UPDATE on the asset row, not an application-level flag or sleep.

    Uses TransactionTestCase (real commits) plus two real threads with
    their own DB connections, so the row lock is genuinely contended
    rather than simulated within a single connection/transaction.
    """

    def setUp(self):
        self.asset = make_asset(tag="RACE-1")
        self.employee_a = make_employee(code="RACE-EMP-A")
        self.employee_b = make_employee(code="RACE-EMP-B")
        client = make_authenticated_client()
        self.auth_header = client._credentials["HTTP_AUTHORIZATION"]

    def test_only_one_checkout_succeeds_for_same_asset(self):
        due_at = (timezone.now() + timedelta(days=1)).isoformat()
        results = {}
        barrier = threading.Barrier(2)

        def attempt(name, employee_code):
            from rest_framework.test import APIClient

            barrier.wait(timeout=5)
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=self.auth_header)
            resp = client.post(
                "/api/v1/checkouts/",
                {
                    "asset_tag": self.asset.asset_tag,
                    "employee_code": employee_code,
                    "due_at": due_at,
                },
                format="json",
            )
            results[name] = resp.status_code
            connection.close()

        t1 = threading.Thread(
            target=attempt, args=("t1", self.employee_a.employee_code)
        )
        t2 = threading.Thread(
            target=attempt, args=("t2", self.employee_b.employee_code)
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(sorted(results.values()), [201, 409])
        self.assertEqual(
            CheckOut.objects.filter(
                asset=self.asset, returned_at__isnull=True
            ).count(),
            1,
        )
