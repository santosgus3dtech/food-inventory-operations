from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import FiscalPnaeException, PnaeCatalog, PnaeCatalogItem
from .services import audit, snapshot


def active_pnae_catalog():
    return PnaeCatalog.objects.filter(is_active=True).first()


def active_pnae_food_ids(catalog=None):
    catalog = catalog or active_pnae_catalog()
    if catalog is None:
        return set()
    return set(
        PnaeCatalogItem.objects.filter(catalog=catalog, is_active=True).values_list(
            "food_id", flat=True
        )
    )


def pnae_item_by_food(catalog=None):
    catalog = catalog or active_pnae_catalog()
    if catalog is None:
        return {}
    result = {}
    for item in PnaeCatalogItem.objects.filter(catalog=catalog, is_active=True).order_by(
        "code", "pk"
    ):
        result.setdefault(item.food_id, item)
    return result


def sync_pnae_exception_requests(prepared_rows, actor):
    catalog = active_pnae_catalog()
    if catalog is None:
        return 0
    catalog_food_ids = active_pnae_food_ids(catalog)
    changed = 0
    for row in prepared_rows:
        item = row["item"]
        food = row.get("food")
        if food is None or food.pk in catalog_food_ids:
            continue
        reason = " ".join(row.get("pnae_exception_reason", "").split())[:500]
        if not reason:
            continue
        exception = FiscalPnaeException.objects.select_for_update().filter(item=item).first()
        if exception is None:
            exception = FiscalPnaeException.objects.create(
                item=item,
                catalog=catalog,
                food=food,
                reason=reason,
                requested_by=actor,
            )
            audit(
                actor,
                exception,
                "pnae_exception_requested",
                description=f"Exceção PNAE solicitada para {item.description}",
            )
            changed += 1
            continue
        if (
            exception.catalog_id == catalog.pk
            and exception.food_id == food.pk
            and exception.reason == reason
        ):
            continue
        before = snapshot(exception)
        exception.catalog = catalog
        exception.food = food
        exception.reason = reason
        exception.status = FiscalPnaeException.Status.PENDING
        exception.requested_by = actor
        exception.requested_at = timezone.now()
        exception.reviewed_by = None
        exception.reviewed_at = None
        exception.save(
            update_fields=[
                "catalog",
                "food",
                "reason",
                "status",
                "requested_by",
                "requested_at",
                "reviewed_by",
                "reviewed_at",
            ]
        )
        audit(
            actor,
            exception,
            "pnae_exception_requested",
            before,
            description=f"Exceção PNAE atualizada para {item.description}",
        )
        changed += 1
    return changed


@transaction.atomic
def approve_pnae_exceptions(prepared_rows, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    catalog = PnaeCatalog.objects.select_for_update().filter(is_active=True).first()
    if catalog is None:
        raise ValidationError("Nenhum catálogo PNAE vigente foi configurado.")
    catalog_food_ids = active_pnae_food_ids(catalog)
    approved = 0
    missing = []
    for row in prepared_rows:
        item = row["item"]
        food = row.get("food")
        if food is None or food.pk in catalog_food_ids:
            continue
        exception = (
            FiscalPnaeException.objects.select_for_update()
            .filter(
                item=item,
                catalog=catalog,
                food=food,
            )
            .first()
        )
        if exception is None or not exception.reason.strip():
            missing.append(item.description)
            continue
        if exception.status == FiscalPnaeException.Status.APPROVED:
            continue
        before = snapshot(exception)
        exception.status = FiscalPnaeException.Status.APPROVED
        exception.reviewed_by = actor
        exception.reviewed_at = timezone.now()
        exception.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        audit(
            actor,
            exception,
            "pnae_exception_approved",
            before,
            description=f"Exceção PNAE aprovada para {item.description}",
        )
        approved += 1
    if missing:
        raise ValidationError("Informe a justificativa PNAE para: " + ", ".join(missing[:3]))
    return approved


def ensure_pnae_compliance(prepared_rows):
    catalog = active_pnae_catalog()
    if catalog is None:
        return
    catalog_food_ids = active_pnae_food_ids(catalog)
    missing = []
    for row in prepared_rows:
        item = row["item"]
        food = row.get("food")
        if food is None or food.pk in catalog_food_ids:
            continue
        approved = FiscalPnaeException.objects.filter(
            item=item,
            catalog=catalog,
            food=food,
            status=FiscalPnaeException.Status.APPROVED,
        ).exists()
        if not approved:
            missing.append(item.description)
    if missing:
        raise ValidationError(
            "Aprovação administrativa PNAE pendente para: " + ", ".join(missing[:3])
        )
