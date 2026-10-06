from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Category, Food, Presentation, StockMovement, Unit, UnitFood, User
from core.stock_services import create_stock_movement

CATALOG = (
    ("Cereais", "ARROZ", "Arroz integral", Food.Measure.KG, "Pacote de 5 kg", Decimal("5")),
    ("Cereais", "FEIJAO", "Feijão carioca", Food.Measure.KG, "Pacote de 1 kg", Decimal("1")),
    ("Laticínios", "LEITE", "Leite integral", Food.Measure.LITRE, "Caixa de 1 L", Decimal("1")),
    ("Hortifruti", "MACA", "Maçã", Food.Measure.KG, "Caixa de 12 kg", Decimal("12")),
    ("Proteínas", "OVOS", "Ovos", Food.Measure.UNIT, "Bandeja com 30", Decimal("30")),
)


class Command(BaseCommand):
    help = "Carrega unidades, alimentos e movimentações totalmente fictícias para o portfólio."

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        local_database = database["ENGINE"].endswith("sqlite3") or database.get("HOST") in {
            "localhost",
            "127.0.0.1",
        }
        if not settings.DEBUG or not local_database:
            raise CommandError("O cenário de portfólio só pode ser criado no ambiente local DEBUG.")
        if Unit.objects.exists():
            raise CommandError("Já existem unidades. Use um banco local vazio para não misturar dados.")
        actor = User.objects.filter(is_active=True, is_general_admin=True).first()
        if actor is None:
            raise CommandError("Crie primeiro o administrador local com: manage.py preparar_local")

        with transaction.atomic():
            foods = []
            for category_name, code, name, measure, pack_name, pack_quantity in CATALOG:
                category, _ = Category.objects.get_or_create(name=category_name)
                food = Food.objects.create(
                    code=code,
                    name=name,
                    category=category,
                    base_unit=measure,
                )
                Presentation.objects.create(food=food, name=pack_name, base_quantity=pack_quantity)
                foods.append(food)

            units = [
                Unit.objects.create(code="DEMO-CENTRO", name="Unidade Demo Centro"),
                Unit.objects.create(code="DEMO-NORTE", name="Unidade Demo Norte"),
                Unit.objects.create(code="DEMO-SUL", name="Unidade Demo Sul"),
            ]
            for unit_index, unit in enumerate(units):
                receipt_lines = []
                for food_index, food in enumerate(foods):
                    quantity = Decimal(18 + unit_index * 4 + food_index * 3)
                    if food.base_unit == Food.Measure.UNIT:
                        quantity = Decimal(90 + unit_index * 30)
                    receipt_lines.append({"food": food, "quantity": quantity, "lot_code": f"DEMO-{unit_index + 1}-{food_index + 1}"})
                create_stock_movement(
                    unit=unit,
                    kind=StockMovement.Kind.RECEIPT,
                    lines=receipt_lines,
                    actor=actor,
                    reason="Entrada sintética do cenário de portfólio",
                )
                create_stock_movement(
                    unit=unit,
                    kind=StockMovement.Kind.CONSUMPTION,
                    lines=[
                        {"food": foods[0], "quantity": Decimal(4 + unit_index)},
                        {"food": foods[2], "quantity": Decimal(6 + unit_index)},
                    ],
                    actor=actor,
                    reason="Consumo sintético de uma semana",
                )
                minimums = {
                    foods[0].pk: Decimal("10"),
                    foods[1].pk: Decimal("8"),
                    foods[2].pk: Decimal("12"),
                    foods[3].pk: Decimal("8"),
                    foods[4].pk: Decimal("60"),
                }
                for unit_food in UnitFood.objects.filter(unit=unit):
                    unit_food.minimum = minimums[unit_food.food_id]
                    unit_food.full_clean()
                    unit_food.save(update_fields=["minimum"])

        self.stdout.write(
            self.style.SUCCESS(
                "Cenário fictício criado: 3 unidades, 5 alimentos e 6 movimentações auditáveis."
            )
        )
