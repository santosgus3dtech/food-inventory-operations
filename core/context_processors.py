from .access import can_import_fiscal_documents


def navigation(request):
    return {
        "current_page": request.resolver_match.url_name if request.resolver_match else "",
        "can_import_nfe": can_import_fiscal_documents(request.user),
        "employee_preview_unit": getattr(request, "employee_preview_unit", None),
        "real_administrator": bool(
            request.user.is_authenticated and request.user.has_administrator_role
        ),
    }
