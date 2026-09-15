from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    """Extends DRF's default handler so a stray IntegrityError (e.g. the
    partial unique constraint on CheckOut firing as a last-resort backstop)
    surfaces as 409 Conflict instead of a 500."""
    response = exception_handler(exc, context)
    if response is not None:
        return response

    if isinstance(exc, IntegrityError):
        return Response({"detail": "conflict"}, status=status.HTTP_409_CONFLICT)

    return None
