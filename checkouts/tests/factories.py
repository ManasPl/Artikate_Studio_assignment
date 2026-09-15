from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from checkouts.models import Asset, Employee


def make_authenticated_client():
    user = User.objects.create_user(username="tester", password="x")
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def make_asset(tag="AST-001", **kwargs):
    defaults = dict(
        name="Test Asset",
        category=Asset.Category.CAMERA,
        purchase_date="2024-01-01",
    )
    defaults.update(kwargs)
    return Asset.objects.create(asset_tag=tag, **defaults)


def make_employee(code="EMP-001", **kwargs):
    defaults = dict(full_name="Test Employee", email=f"{code.lower()}@example.com")
    defaults.update(kwargs)
    return Employee.objects.create(employee_code=code, **defaults)
