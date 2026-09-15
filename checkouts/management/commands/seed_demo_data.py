from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from checkouts.models import Asset, CheckOut, Employee


class Command(BaseCommand):
    help = (
        "Populates the database with demo assets, employees and check-outs "
        "covering every business rule (overdue, on-time return, late "
        "return, available, in maintenance). Safe to re-run."
    )

    def handle(self, *args, **options):
        with transaction.atomic():
            assets = self._seed_assets()
            employees = self._seed_employees()
            self._seed_checkouts(assets, employees)

        self.stdout.write(self.style.SUCCESS(
            f"Seed complete: {Asset.objects.count()} assets, "
            f"{Employee.objects.count()} employees, "
            f"{CheckOut.objects.count()} check-outs."
        ))

    def _seed_assets(self):
        rows = [
            ("CAM-1001", "Sony A7 IV", Asset.Category.CAMERA, "2023-03-10"),
            ("CAM-1002", "Canon EOS R6", Asset.Category.CAMERA, "2023-05-22"),
            ("LAP-2001", "Dell XPS 15", Asset.Category.LAPTOP, "2022-11-01"),
            ("LAP-2002", "MacBook Pro 14", Asset.Category.LAPTOP, "2023-01-15"),
            ("LAP-2003", "ThinkPad X1", Asset.Category.LAPTOP, "2023-07-19"),
            ("SEN-3001", "Temperature Logger", Asset.Category.SENSOR, "2024-02-02"),
            ("SEN-3002", "Vibration Sensor", Asset.Category.SENSOR, "2024-04-18"),
            ("VEH-4001", "Field Survey Van", Asset.Category.VEHICLE, "2021-09-30"),
        ]
        assets = {}
        for tag, name, category, purchase_date in rows:
            asset, _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults=dict(name=name, category=category, purchase_date=purchase_date),
            )
            assets[tag] = asset
        return assets

    def _seed_employees(self):
        rows = [
            ("EMP-001", "Asha Verma", "asha.verma@example.com", True),
            ("EMP-002", "Rohit Sharma", "rohit.sharma@example.com", True),
            ("EMP-003", "Priya Nair", "priya.nair@example.com", True),
            ("EMP-004", "Karan Mehta", "karan.mehta@example.com", False),
        ]
        employees = {}
        for code, full_name, email, is_active in rows:
            employee, _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults=dict(full_name=full_name, email=email, is_active=is_active),
            )
            employees[code] = employee
        return employees

    def _seed_checkout(self, asset, employee, days_ago_checked_out, due_at,
                        returned_at=None, condition_note="", needs_maintenance=False):
        """get_or_create keyed on (asset, employee) so re-running the
        command never tries to open a second check-out against an asset
        that already has one from a prior run (which would trip the
        partial unique constraint) or duplicate a historical return."""
        checkout, created = CheckOut.objects.get_or_create(
            asset=asset,
            employee=employee,
            defaults=dict(
                due_at=due_at,
                returned_at=returned_at,
                condition_note=condition_note,
            ),
        )
        if created:
            CheckOut.objects.filter(pk=checkout.pk).update(
                checked_out_at=timezone.now() - timedelta(days=days_ago_checked_out)
            )
            checkout.refresh_from_db()

        expected_status = (
            Asset.Status.MAINTENANCE
            if needs_maintenance
            else (Asset.Status.AVAILABLE if checkout.returned_at else Asset.Status.CHECKED_OUT)
        )
        if asset.status != expected_status:
            asset.status = expected_status
            asset.save(update_fields=["status", "updated_at"])

        return checkout

    def _seed_checkouts(self, assets, employees):
        now = timezone.now()

        # Currently overdue (2).
        self._seed_checkout(
            assets["CAM-1001"], employees["EMP-001"],
            days_ago_checked_out=10, due_at=now - timedelta(days=3),
        )
        self._seed_checkout(
            assets["LAP-2001"], employees["EMP-002"],
            days_ago_checked_out=12, due_at=now - timedelta(days=5),
        )

        # Returned on time (2): returned_at before due_at.
        self._seed_checkout(
            assets["CAM-1002"], employees["EMP-001"],
            days_ago_checked_out=15, due_at=now - timedelta(days=8),
            returned_at=now - timedelta(days=9),
            condition_note="Returned in good condition.",
        )
        self._seed_checkout(
            assets["LAP-2002"], employees["EMP-003"],
            days_ago_checked_out=20, due_at=now - timedelta(days=13),
            returned_at=now - timedelta(days=14),
            condition_note="Returned in good condition.",
        )

        # Returned late (1): returned_at after due_at.
        self._seed_checkout(
            assets["LAP-2003"], employees["EMP-002"],
            days_ago_checked_out=9, due_at=now - timedelta(days=6),
            returned_at=now - timedelta(days=2),
            condition_note="Minor scuffs on the lid; returned a few days late.",
        )

        # SEN-3001 and VEH-4001 stay AVAILABLE (never checked out).
        # SEN-3002 is set to MAINTENANCE directly, with no check-out history.
        sensor = assets["SEN-3002"]
        if sensor.status != Asset.Status.MAINTENANCE:
            sensor.status = Asset.Status.MAINTENANCE
            sensor.save(update_fields=["status", "updated_at"])
