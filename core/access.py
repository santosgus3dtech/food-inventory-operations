from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import Unit, UnitAccess


def administrator_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_administrator:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def visible_units(user):
    preview_unit_id = getattr(user, "_employee_preview_unit_id", None)
    if preview_unit_id:
        return Unit.objects.filter(pk=preview_unit_id, is_active=True)
    if user.is_administrator:
        return Unit.objects.all()
    return Unit.objects.filter(is_active=True, unitaccess__user=user, unitaccess__is_active=True)


def get_unit(user, pk):
    return get_object_or_404(visible_units(user), pk=pk)


def can_manage_unit(user, unit):
    return user.is_administrator or (
        unit.is_active
        and UnitAccess.objects.filter(
            user=user, unit=unit, is_active=True, role=UnitAccess.Role.MANAGER
        ).exists()
    )


def manageable_units(user):
    if getattr(user, "_employee_preview_unit_id", None):
        return Unit.objects.none()
    if user.is_administrator:
        return Unit.objects.filter(is_active=True)
    return Unit.objects.filter(
        is_active=True,
        unitaccess__user=user,
        unitaccess__is_active=True,
        unitaccess__role=UnitAccess.Role.MANAGER,
    ).distinct()


def can_import_fiscal_documents(user):
    return user.is_authenticated and manageable_units(user).exists()


def require_catalog_access(user):
    if not user.is_administrator and not visible_units(user).exists():
        raise PermissionDenied
