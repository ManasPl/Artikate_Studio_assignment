from django.db import connection, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Value
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
    CheckOutSerializer,
)


class AssetListCreateView(generics.ListCreateAPIView):
    queryset = Asset.objects.all().order_by("id")
    serializer_class = AssetSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]


class AssetDetailView(generics.RetrieveAPIView):
    queryset = Asset.objects.all()
    serializer_class = AssetSerializer


class CheckOutCreateView(APIView):
    """POST /checkouts/ — applies business rules 1-5, 7 and 8.

    Concurrency (rule 7) is enforced with SELECT ... FOR UPDATE on the
    Asset row inside a transaction: two simultaneous requests for the same
    asset serialize on that row lock, so the second one to acquire it
    re-reads status as CHECKED_OUT and gets 409. The partial unique
    constraint on CheckOut (see models.py) is a DB-level backstop in case
    that lock is ever bypassed.
    """

    def post(self, request):
        serializer = CheckOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        employee = get_object_or_404(Employee, employee_code=data["employee_code"])
        # 404 up front for an unknown asset_tag before we open the transaction.
        get_object_or_404(Asset, asset_tag=data["asset_tag"])

        if not employee.is_active:
            return Response(
                {"detail": "employee is not active"}, status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Lock order is always asset -> employee to avoid deadlocking
            # against another request locking the same two rows in reverse.
            asset = Asset.objects.select_for_update().get(
                asset_tag=data["asset_tag"]
            )
            employee = Employee.objects.select_for_update().get(pk=employee.pk)

            if asset.status != Asset.Status.AVAILABLE:
                return Response(
                    {"detail": "asset is not available"},
                    status=status.HTTP_409_CONFLICT,
                )

            open_count = CheckOut.objects.filter(
                employee=employee, returned_at__isnull=True
            ).count()
            if open_count >= 3:
                return Response(
                    {"detail": "employee already holds 3 open check-outs"},
                    status=status.HTTP_409_CONFLICT,
                )

            checkout = CheckOut.objects.create(
                asset=asset,
                employee=employee,
                due_at=data["due_at"],
            )
            asset.status = Asset.Status.CHECKED_OUT
            asset.save(update_fields=["status", "updated_at"])

        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED)


class CheckOutReturnView(APIView):
    def post(self, request, pk):
        get_object_or_404(CheckOut, pk=pk)

        serializer = CheckOutReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            checkout = CheckOut.objects.select_for_update().get(pk=pk)
            if checkout.returned_at is not None:
                return Response(
                    {"detail": "check-out already returned"},
                    status=status.HTTP_409_CONFLICT,
                )

            checkout.returned_at = timezone.now()
            checkout.condition_note = data["condition_note"]
            checkout.save(update_fields=["returned_at", "condition_note"])

            asset = Asset.objects.select_for_update().get(pk=checkout.asset_id)
            asset.status = (
                Asset.Status.MAINTENANCE
                if data["needs_maintenance"]
                else Asset.Status.AVAILABLE
            )
            asset.save(update_fields=["status", "updated_at"])

        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_200_OK)


class EmployeeSummaryView(APIView):
    """GET /employees/{employee_code}/summary/ — four numbers computed by
    the database in a single aggregate query."""

    def get(self, request, employee_code):
        employee = get_object_or_404(Employee, employee_code=employee_code)
        now = timezone.now()

        hold_duration = ExpressionWrapper(
            F("returned_at") - F("checked_out_at"), output_field=DurationField()
        )

        summary = CheckOut.objects.filter(employee=employee).aggregate(
            lifetime_checkout_count=Count("id"),
            currently_held=Count("id", filter=Q(returned_at__isnull=True)),
            currently_overdue=Count(
                "id", filter=Q(returned_at__isnull=True, due_at__lt=now)
            ),
            mean_hold_duration=Avg(hold_duration, filter=Q(returned_at__isnull=False)),
        )

        mean_hold_duration = summary["mean_hold_duration"]
        mean_hold_duration_days = (
            round(mean_hold_duration.total_seconds() / 86400, 2)
            if mean_hold_duration is not None
            else None
        )

        return Response(
            {
                "employee_code": employee.employee_code,
                "full_name": employee.full_name,
                "lifetime_checkout_count": summary["lifetime_checkout_count"],
                "currently_held": summary["currently_held"],
                "currently_overdue": summary["currently_overdue"],
                "mean_hold_duration_days": mean_hold_duration_days,
            }
        )


class OverdueReportView(generics.ListAPIView):
    """GET /reports/overdue/ — a single annotated, select_related query;
    no query is issued per row."""

    def get_queryset(self):
        now = timezone.now()
        overdue_duration = ExpressionWrapper(
            Value(now) - F("due_at"), output_field=DurationField()
        )
        return (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
            .select_related("asset", "employee")
            .annotate(overdue_duration=overdue_duration)
            .order_by("due_at")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = page if page is not None else queryset

        rows = [
            {
                "checkout_id": c.id,
                "asset_name": c.asset.name,
                "asset_tag": c.asset.asset_tag,
                "employee_code": c.employee.employee_code,
                "employee_name": c.employee.full_name,
                "due_at": c.due_at,
                "days_overdue": c.overdue_duration.days,
            }
            for c in items
        ]

        if page is not None:
            return self.get_paginated_response(rows)
        return Response({"count": len(rows), "results": rows})


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        db_ok = True
        try:
            connection.ensure_connection()
        except Exception:
            db_ok = False

        return Response(
            {"status": "ok" if db_ok else "degraded", "database": db_ok},
            status=status.HTTP_200_OK,
        )
