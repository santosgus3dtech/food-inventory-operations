from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils import timezone

from .models import Unit


class EmployeePreviewMiddleware:
    """Applies an employee-scoped, read-only view to an administrator session."""

    allowed_post_suffixes = ("/visualizar-como-funcionario/sair/", "/sair/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        preview_unit_id = request.session.get("employee_preview_unit_id")
        if preview_unit_id and request.user.is_authenticated:
            if not request.user.has_administrator_role:
                request.session.pop("employee_preview_unit_id", None)
            else:
                unit = Unit.objects.filter(pk=preview_unit_id, is_active=True).first()
                if unit is None:
                    request.session.pop("employee_preview_unit_id", None)
                else:
                    request.user._employee_preview_unit_id = unit.pk
                    request.employee_preview_unit = unit
                    if request.method == "POST" and not request.path_info.endswith(
                        self.allowed_post_suffixes
                    ):
                        from django.http import HttpResponseForbidden

                        return HttpResponseForbidden(
                            "A visualização como funcionário é somente leitura."
                        )
        return self.get_response(request)


class AccountPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.must_change_password:
            expiry = request.user.temporary_password_expires_at
            if expiry and expiry <= timezone.now():
                logout(request)
                messages.error(
                    request, "A senha temporária expirou. Solicite uma nova à administração."
                )
                return redirect("login")
            if request.path_info not in ("/senha/", "/sair/") and not request.path_info.startswith(
                "/static/"
            ):
                return redirect("password_change")
        response = self.get_response(request)
        if request.user.is_authenticated:
            response["Cache-Control"] = "no-store, private"
        return response
