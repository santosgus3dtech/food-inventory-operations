from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .access import can_manage_unit, visible_units
from .models import Food, StockMovement, StockMovementItem, Unit, UnitFood
from .services import audit

MOVEMENT_DIRECTIONS = {
    StockMovement.Kind.RECEIPT: Decimal("1"),
    StockMovement.Kind.CONSUMPTION: Decimal("-1"),
    StockMovement.Kind.LOSS: Decimal("-1"),
    StockMovement.Kind.SUPPLIER_RETURN: Decimal("-1"),
    StockMovement.Kind.STOCK_RETURN: Decimal("1"),
    StockMovement.Kind.ADJUSTMENT_IN: Decimal("1"),
    StockMovement.Kind.ADJUSTMENT_OUT: Decimal("-1"),
}
OPERATOR_KINDS = {
    StockMovement.Kind.RECEIPT,
    StockMovement.Kind.CONSUMPTION,
    StockMovement.Kind.LOSS,
    StockMovement.Kind.STOCK_RETURN,
}
RESTRICTED_KINDS = {
    StockMovement.Kind.SUPPLIER_RETURN,
    StockMovement.Kind.ADJUSTMENT_IN,
    StockMovement.Kind.ADJUSTMENT_OUT,
}


def can_create_movement(user, unit, kind):
    if kind not in MOVEMENT_DIRECTIONS:
        return False
    if not visible_units(user).filter(pk=unit.pk).exists():
        return False
    return kind in OPERATOR_KINDS or can_manage_unit(user, unit)


def movement_kind_choices(user, unit=None):
    choices = []
    for value, label in StockMovement.Kind.choices:
        if value not in MOVEMENT_DIRECTIONS:
            continue
        if unit is not None:
            allowed = can_create_movement(user, unit, value)
        else:
            allowed = value in OPERATOR_KINDS or user.is_administrator
            if value in RESTRICTED_KINDS and not allowed:
                allowed = any(can_manage_unit(user, candidate) for candidate in visible_units(user))
        if allowed:
            choices.append((value, label))
    return choices


def _clean_quantity(food, value):
    quantity = Decimal(value)
    if quantity <= 0:
        raise ValidationError("A quantidade deve ser maior que zero.")
    if food.base_unit == Food.Measure.UNIT and quantity != quantity.to_integral_value():
        raise ValidationError(f"{food.name} exige uma quantidade inteira.")
    return quantity


@transaction.atomic
def create_stock_movement(
    *,
    unit,
    kind,
    lines,
    actor,
    occurred_at=None,
    reason="",
    fiscal_document=None,
):
    if not can_create_movement(actor, unit, kind):
        raise PermissionDenied
    direction = MOVEMENT_DIRECTIONS[kind]
    prepared = []
    for line in lines:
        food = line["food"]
        quantity = _clean_quantity(food, line["quantity"])
        prepared.append({**line, "food": food, "delta": quantity * direction})
    return _apply_stock_movement(
        unit=unit,
        kind=kind,
        lines=prepared,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        fiscal_document=fiscal_document,
    )


def _apply_stock_movement(
    *,
    unit,
    kind,
    lines,
    actor,
    occurred_at=None,
    reason="",
    fiscal_document=None,
    reversed_movement=None,
):
    if not lines:
        raise ValidationError("Inclua pelo menos um alimento na movimentação.")
    unit = Unit.objects.select_for_update().get(pk=unit.pk, is_active=True)
    food_ids = {line["food"].pk for line in lines}
    foods = {
        food.pk: food
        for food in Food.objects.select_for_update().filter(pk__in=food_ids, is_active=True)
    }
    if set(foods) != food_ids:
        raise ValidationError("Um dos alimentos selecionados foi desativado.")

    unit_foods = {
        row.food_id: row
        for row in UnitFood.objects.select_for_update().filter(unit=unit, food_id__in=food_ids)
    }
    totals = {}
    for line in lines:
        food = foods[line["food"].pk]
        unit_food = unit_foods.get(food.pk)
        if unit_food is None:
            if line["delta"] < 0:
                raise ValidationError(f"{food.name} ainda não possui saldo nesta unidade.")
            unit_food = UnitFood(unit=unit, food=food, minimum=Decimal("0"), is_active=True)
            unit_food.full_clean()
            unit_food.save()
            unit_foods[food.pk] = unit_food
        if not unit_food.is_active:
            raise ValidationError(f"{food.name} não está sendo acompanhado nesta unidade.")
        totals[food.pk] = totals.get(food.pk, Decimal("0")) + line["delta"]

    for food_id, delta in totals.items():
        food = foods[food_id]
        unit_food = unit_foods[food_id]
        new_quantity = unit_food.quantity + delta
        if new_quantity < 0:
            raise ValidationError(
                f"Saldo insuficiente de {food.name}. Disponível: "
                f"{unit_food.quantity} {food.base_unit}."
            )

    movement = StockMovement.objects.create(
        unit=unit,
        kind=kind,
        occurred_at=occurred_at or timezone.now(),
        reason=" ".join(reason.split()),
        created_by=actor,
        fiscal_document=fiscal_document,
        reversed_movement=reversed_movement,
    )
    for line in lines:
        food = foods[line["food"].pk]
        unit_food = unit_foods[food.pk]
        unit_food.quantity += line["delta"]
        unit_food.full_clean()
        unit_food.save(update_fields=["quantity"])
        StockMovementItem.objects.create(
            movement=movement,
            unit_food=unit_food,
            delta=line["delta"],
            lot_code=" ".join(line.get("lot_code", "").split()),
            expires_on=line.get("expires_on"),
            fiscal_document_item=line.get("fiscal_document_item"),
        )
    audit(
        actor,
        movement,
        "movement_created",
        unit=unit,
        description=f"{movement.get_kind_display()} registrada em {unit.code}",
    )
    return movement


@transaction.atomic
def reverse_stock_movement(*, movement, actor, reason):
    original = StockMovement.objects.select_for_update().select_related("unit").get(pk=movement.pk)
    if not can_manage_unit(actor, original.unit):
        raise PermissionDenied
    if original.kind in {StockMovement.Kind.OPENING, StockMovement.Kind.REVERSAL}:
        raise ValidationError("Este tipo de movimentação não pode ser estornado.")
    if StockMovement.objects.filter(reversed_movement=original).exists():
        raise ValidationError("Esta movimentação já foi estornada.")
    lines = [
        {
            "food": item.unit_food.food,
            "delta": -item.delta,
            "lot_code": item.lot_code,
            "expires_on": item.expires_on,
        }
        for item in original.items.select_related("unit_food__food")
    ]
    return _apply_stock_movement(
        unit=original.unit,
        kind=StockMovement.Kind.REVERSAL,
        lines=lines,
        actor=actor,
        reason=reason,
        reversed_movement=original,
    )
