from django.db import migrations
from django.utils import timezone


def create_opening_movements(apps, schema_editor):
    UnitFood = apps.get_model("core", "UnitFood")
    StockMovement = apps.get_model("core", "StockMovement")
    StockMovementItem = apps.get_model("core", "StockMovementItem")
    rows = UnitFood.objects.filter(quantity__gt=0).order_by("unit_id", "pk")
    movement_by_unit = {}
    now = timezone.now()
    for row in rows.iterator():
        movement = movement_by_unit.get(row.unit_id)
        if movement is None:
            movement = StockMovement.objects.create(
                unit_id=row.unit_id,
                kind="opening",
                occurred_at=now,
                reason="Saldo existente antes da implantação das movimentações.",
                created_by_id=None,
            )
            movement_by_unit[row.unit_id] = movement
        StockMovementItem.objects.create(
            movement_id=movement.pk,
            unit_food_id=row.pk,
            delta=row.quantity,
        )


PROTECT_LEDGER_SQL = """
CREATE OR REPLACE FUNCTION core_protect_stock_ledger()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Movimentações confirmadas não podem ser alteradas ou apagadas.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_stockmovement_immutable
BEFORE UPDATE OR DELETE ON core_stockmovement
FOR EACH ROW EXECUTE FUNCTION core_protect_stock_ledger();

CREATE TRIGGER core_stockmovementitem_immutable
BEFORE UPDATE OR DELETE ON core_stockmovementitem
FOR EACH ROW EXECUTE FUNCTION core_protect_stock_ledger();
"""

UNPROTECT_LEDGER_SQL = """
DROP TRIGGER IF EXISTS core_stockmovementitem_immutable ON core_stockmovementitem;
DROP TRIGGER IF EXISTS core_stockmovement_immutable ON core_stockmovement;
DROP FUNCTION IF EXISTS core_protect_stock_ledger();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0005_fiscaldocumentitem_expires_on_and_more")]

    operations = [
        migrations.RunPython(create_opening_movements, migrations.RunPython.noop),
        migrations.RunSQL(PROTECT_LEDGER_SQL, UNPROTECT_LEDGER_SQL),
    ]
