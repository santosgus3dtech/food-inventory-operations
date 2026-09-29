from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction

from .models import AuditEvent, User

_AUTO_SNAPSHOT = object()

# Explicit allowlist: credentials and session state never enter event payloads.
AUDITED_FIELDS = {
    "unit": ["code", "name", "fiscal_address_match", "is_active"],
    "category": ["name", "is_active"],
    "food": [
        "code",
        "name",
        "category_id",
        "base_unit",
        "requires_expiry",
        "image_path",
        "is_active",
    ],
    "presentation": ["food_id", "name", "base_quantity", "is_active"],
    "pnaecatalog": [
        "year",
        "title",
        "reference",
        "source_name",
        "source_url",
        "is_active",
    ],
    "pnaecatalogitem": [
        "catalog_id",
        "code",
        "name",
        "specification",
        "acquisition_unit",
        "packaging",
        "source_page",
        "food_id",
        "is_active",
    ],
    "unitfood": ["unit_id", "food_id", "minimum", "quantity", "is_active"],
    "user": [
        "username",
        "first_name",
        "last_name",
        "email",
        "is_active",
        "is_general_admin",
        "registration_status",
    ],
    "unitaccess": ["user_id", "unit_id", "role", "is_active"],
    "fiscaldocument": [
        "access_key",
        "number",
        "series",
        "supplier_cnpj",
        "supplier_name",
        "total",
        "unit_id",
        "status",
    ],
    "fiscalpdf": [
        "access_key",
        "original_name",
        "page_count",
        "document_id",
        "status",
    ],
    "menudocument": [
        "original_name",
        "title",
        "week_start",
        "week_end",
        "nutritionist_name",
        "nutritionist_registration",
        "page_count",
        "status",
    ],
    "weeklymenuitem": [
        "service_date",
        "meal_type",
        "source_meal_id",
        "description",
        "created_by_id",
        "updated_by_id",
        "created_at",
        "updated_at",
    ],
    "disposalrecord": [
        "unit_id",
        "occurred_on",
        "observation",
        "created_by_id",
        "created_at",
    ],
    "fiscalpnaeexception": [
        "item_id",
        "catalog_id",
        "food_id",
        "reason",
        "status",
        "requested_by_id",
        "requested_at",
        "reviewed_by_id",
        "reviewed_at",
    ],
    "stockmovement": [
        "unit_id",
        "kind",
        "occurred_at",
        "reason",
        "fiscal_document_id",
        "reversed_movement_id",
    ],
}


def snapshot(obj):
    values = {}
    for field in AUDITED_FIELDS[obj._meta.model_name]:
        value = getattr(obj, field)
        values[field] = str(value) if isinstance(value, (Decimal, date, datetime)) else value
    return values


def audit(actor, obj, action, before=None, unit=None, description=None, after=_AUTO_SNAPSHOT):
    if after is _AUTO_SNAPSHOT:
        after = {} if action.startswith("password") else snapshot(obj)
    return AuditEvent.objects.create(
        actor=actor,
        actor_name=actor.display_name if actor else "Sistema",
        unit=unit,
        action=action,
        entity=obj._meta.model_name,
        entity_id=str(obj.pk),
        description=description or str(obj)[:240],
        before=before or {},
        after=after,
    )


def lock_user_management():
    # Serializes last-admin checks and access changes, including creation races.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [72834101])


@transaction.atomic
def save_user(form, actor):
    lock_user_management()
    obj = form.instance
    existing = User.objects.select_for_update().filter(pk=obj.pk).first() if obj.pk else None
    before = snapshot(existing) if existing else {}
    if existing and existing.pk == actor.pk and (not obj.is_active or not obj.is_general_admin):
        raise ValidationError(
            "Você não pode desativar ou retirar seu próprio acesso de administrador."
        )
    if existing and existing.is_superuser:
        raise ValidationError("Contas técnicas não podem ser alteradas por esta tela.")
    if existing and existing.is_general_admin and (not obj.is_active or not obj.is_general_admin):
        if (
            not User.objects.filter(is_active=True, is_general_admin=True)
            .exclude(pk=obj.pk)
            .exists()
        ):
            raise ValidationError("Mantenha pelo menos um administrador geral ativo.")
    if existing:
        if (obj.is_active, obj.is_general_admin) != (existing.is_active, existing.is_general_admin):
            obj.session_version = existing.session_version + 1
    obj = form.save()
    audit(
        actor,
        obj,
        "updated" if existing else "created",
        before,
        description=f"Usuário: {obj.display_name}",
    )
    return obj
