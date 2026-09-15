from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Asset, CheckOut, Employee


class AssetSerializer(serializers.ModelSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
            "current_holder",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def get_current_holder(self, obj):
        open_checkout = (
            obj.checkouts.filter(returned_at__isnull=True)
            .select_related("employee")
            .first()
        )
        if open_checkout is None:
            return None
        return {
            "employee_code": open_checkout.employee.employee_code,
            "full_name": open_checkout.employee.full_name,
        }


class EmployeeMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["employee_code", "full_name"]


class AssetMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ["asset_tag", "name"]


class CheckOutSerializer(serializers.ModelSerializer):
    asset = AssetMiniSerializer(read_only=True)
    employee = EmployeeMiniSerializer(read_only=True)

    class Meta:
        model = CheckOut
        fields = [
            "id",
            "asset",
            "employee",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        ]


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField()
    employee_code = serializers.CharField()
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=30):
            raise serializers.ValidationError(
                "due_at must be no more than 30 days from now."
            )
        return value


class CheckOutReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True, default="")
    needs_maintenance = serializers.BooleanField(required=False, default=False)
