from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from .models import MenuDocument, MenuMeal, WeeklyMenuItem
from .services import audit, snapshot


def _safe_filename(value):
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return value[:180] or "cardapio.pdf"


@transaction.atomic
def create_menu_document(parsed, original_name, actor):
    if not actor.has_administrator_role:
        raise PermissionDenied

    existing = MenuDocument.objects.filter(pdf_sha256=parsed.pdf_sha256).first()
    if existing:
        return existing, False

    status = (
        MenuDocument.Status.PROCESSED
        if parsed.week_start and parsed.week_end and parsed.meals
        else MenuDocument.Status.NEEDS_REVIEW
    )
    try:
        with transaction.atomic():
            document = MenuDocument.objects.create(
                original_name=_safe_filename(original_name),
                title=parsed.title,
                week_start=parsed.week_start,
                week_end=parsed.week_end,
                nutritionist_name=parsed.nutritionist_name,
                nutritionist_registration=parsed.nutritionist_registration,
                notes=parsed.notes,
                page_count=parsed.page_count,
                pdf_sha256=parsed.pdf_sha256,
                pdf_content=parsed.pdf_content,
                source_text=parsed.source_text,
                warnings=list(parsed.warnings),
                status=status,
                created_by=actor,
            )
    except IntegrityError:
        document = MenuDocument.objects.get(pdf_sha256=parsed.pdf_sha256)
        return document, False

    MenuMeal.objects.bulk_create(
        [
            MenuMeal(
                document=document,
                service_date=meal.service_date,
                meal_type=meal.meal_type,
                description=meal.description,
            )
            for meal in parsed.meals
        ]
    )
    period = (
        f"{document.week_start:%d/%m/%Y} a {document.week_end:%d/%m/%Y}"
        if document.week_start and document.week_end
        else "período não identificado"
    )
    audit(
        actor,
        document,
        "menu_uploaded",
        description=f"Cardápio {period}: {len(parsed.meals)} referências extraídas",
    )
    return document, True


@transaction.atomic
def set_weekly_menu_item(*, service_date, meal_type, source_meal, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    if service_date.weekday() > 4:
        raise ValidationError("Escolha um dia útil da semana.")

    source_meal = MenuMeal.objects.select_for_update().get(pk=source_meal.pk)
    if source_meal.meal_type != meal_type:
        raise ValidationError("Escolha um prato da refeição indicada.")

    item = (
        WeeklyMenuItem.objects.select_for_update()
        .filter(
            service_date=service_date,
            meal_type=meal_type,
        )
        .first()
    )
    before = snapshot(item) if item else {}
    created = item is None
    if created:
        item = WeeklyMenuItem.objects.create(
            service_date=service_date,
            meal_type=meal_type,
            source_meal=source_meal,
            description=source_meal.description.strip(),
            created_by=actor,
            updated_by=actor,
        )
    else:
        item.source_meal = source_meal
        item.description = source_meal.description.strip()
        item.updated_by = actor
        item.save(update_fields=["source_meal", "description", "updated_by", "updated_at"])

    audit(
        actor,
        item,
        "menu_item_created" if created else "menu_item_updated",
        before=before,
        description=(f"Cardápio de {service_date:%d/%m/%Y}: {item.get_meal_type_display()}"),
    )
    return item, created


@transaction.atomic
def delete_weekly_menu_item(*, item, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    item = WeeklyMenuItem.objects.select_for_update().get(pk=item.pk)
    before = snapshot(item)
    description = f"Cardápio de {item.service_date:%d/%m/%Y}: {item.get_meal_type_display()}"
    audit(
        actor,
        item,
        "menu_item_deleted",
        before=before,
        after={},
        description=description,
    )
    item.delete()
