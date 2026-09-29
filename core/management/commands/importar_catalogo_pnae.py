import json
import re
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import (
    Category,
    Food,
    PnaeCatalog,
    PnaeCatalogItem,
    Presentation,
    StockMovementItem,
    Unit,
    UnitFood,
    normalize_name,
)
from core.services import audit, snapshot

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "pnae_2026.json"


class Command(BaseCommand):
    help = "Importa o catálogo PNAE 2026 e vincula seus alimentos às unidades."

    def add_arguments(self, parser):
        parser.add_argument(
            "--actor",
            help="Usuário administrador responsável pela importação em produção.",
        )
        parser.add_argument("--json", action="store_true", help="Exibe o resumo em JSON.")

    def _actor(self, username):
        if not username:
            return None
        try:
            actor = get_user_model().objects.get(username=username.strip().lower())
        except get_user_model().DoesNotExist as exc:
            raise CommandError(f"Usuário {username!r} não encontrado.") from exc
        if not actor.is_administrator:
            raise CommandError("O responsável informado precisa ser administrador.")
        return actor

    @staticmethod
    def _next_food_code(existing_codes, sequence):
        code = f"AL{sequence:03d}"
        while code in existing_codes:
            sequence += 1
            code = f"AL{sequence:03d}"
        return code, sequence + 1

    @staticmethod
    def _can_change_measure(food):
        return not (
            UnitFood.objects.filter(food=food).exclude(quantity=0).exists()
            or StockMovementItem.objects.filter(unit_food__food=food).exists()
        )

    @transaction.atomic
    def handle(self, *args, **options):
        actor = self._actor(options.get("actor"))
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if len(data["items"]) != 112:
            raise CommandError("O arquivo PNAE 2026 deve conter exatamente 112 itens.")

        catalogs = list(PnaeCatalog.objects.select_for_update())
        for other in catalogs:
            if other.year != data["year"] and other.is_active:
                before = snapshot(other)
                other.is_active = False
                other.save(update_fields=["is_active"])
                audit(actor, other, "updated", before)

        catalog = next((item for item in catalogs if item.year == data["year"]), None)
        catalog_values = {
            "title": data["title"],
            "reference": data["reference"],
            "source_name": data["source_name"],
            "source_url": data["source_url"],
            "is_active": True,
        }
        if catalog is None:
            catalog = PnaeCatalog.objects.create(year=data["year"], **catalog_values)
            audit(actor, catalog, "created")
        else:
            before = snapshot(catalog)
            changed = False
            for field, value in catalog_values.items():
                if getattr(catalog, field) != value:
                    setattr(catalog, field, value)
                    changed = True
            if changed:
                catalog.save(update_fields=list(catalog_values))
                audit(actor, catalog, "updated", before)

        categories = {}
        for category_name in sorted({item["category"] for item in data["items"]}):
            category = Category.objects.filter(
                normalized_name=normalize_name(category_name)
            ).first()
            if category is None:
                category = Category.objects.create(name=category_name, is_active=True)
                audit(actor, category, "created")
            categories[category_name] = category

        foods = list(Food.objects.select_for_update().select_related("category"))
        foods_by_name = {food.normalized_name: food for food in foods}
        existing_codes = {food.code for food in foods}
        sequence = (
            max(
                (
                    int(match.group(1))
                    for code in existing_codes
                    if (match := re.fullmatch(r"AL(\d+)", code))
                ),
                default=0,
            )
            + 1
        )
        units = list(Unit.objects.select_for_update().order_by("pk"))
        summary = {
            "catalog_year": catalog.year,
            "catalog_items_created": 0,
            "catalog_items_updated": 0,
            "foods_matched": 0,
            "foods_created": 0,
            "food_measures_corrected": 0,
            "measure_conflicts": [],
            "presentations_created": 0,
            "unit_food_links_created": 0,
            "units": len(units),
        }

        for source in data["items"]:
            lookup_names = [source.get("existing_food_name"), source["food_name"]]
            food = next(
                (
                    foods_by_name.get(normalize_name(name))
                    for name in lookup_names
                    if name and foods_by_name.get(normalize_name(name))
                ),
                None,
            )
            if food is None:
                code, sequence = self._next_food_code(existing_codes, sequence)
                food = Food(
                    code=code,
                    name=source["food_name"],
                    category=categories[source["category"]],
                    base_unit=source["base_unit"],
                    requires_expiry=True,
                    is_active=True,
                )
                food.full_clean()
                food.save()
                audit(actor, food, "created")
                existing_codes.add(code)
                foods_by_name[food.normalized_name] = food
                summary["foods_created"] += 1
            else:
                summary["foods_matched"] += 1
                if food.base_unit != source["base_unit"]:
                    if self._can_change_measure(food):
                        before = snapshot(food)
                        food.base_unit = source["base_unit"]
                        food.save(update_fields=["base_unit"])
                        audit(
                            actor,
                            food,
                            "pnae_measure_corrected",
                            before,
                            description=(
                                f"Medida de {food.code} corrigida conforme PNAE {catalog.year}"
                            ),
                        )
                        summary["food_measures_corrected"] += 1
                    else:
                        summary["measure_conflicts"].append(
                            {
                                "food": food.code,
                                "current": food.base_unit,
                                "pnae": source["base_unit"],
                            }
                        )

            for unit in units:
                link, created = UnitFood.objects.get_or_create(
                    unit=unit,
                    food=food,
                    defaults={"minimum": Decimal("0"), "quantity": Decimal("0"), "is_active": True},
                )
                if created:
                    audit(actor, link, "created", unit=unit)
                    summary["unit_food_links_created"] += 1

            presentation_data = source.get("presentation")
            if presentation_data and food.base_unit == source["base_unit"]:
                normalized_presentation = normalize_name(presentation_data["name"])
                presentation = Presentation.objects.filter(
                    food=food, normalized_name=normalized_presentation
                ).first()
                if presentation is None:
                    presentation = Presentation(
                        food=food,
                        name=presentation_data["name"],
                        base_quantity=Decimal(presentation_data["base_quantity"]),
                        is_active=True,
                    )
                    presentation.full_clean()
                    presentation.save()
                    audit(actor, presentation, "created")
                    summary["presentations_created"] += 1

            item_values = {
                "name": source["name"],
                "specification": source["specification"],
                "acquisition_unit": source["acquisition_unit"],
                "packaging": source["packaging"],
                "source_page": source["source_page"],
                "food": food,
                "is_active": True,
            }
            item = PnaeCatalogItem.objects.filter(catalog=catalog, code=source["code"]).first()
            if item is None:
                item = PnaeCatalogItem.objects.create(
                    catalog=catalog,
                    code=source["code"],
                    **item_values,
                )
                audit(actor, item, "created")
                summary["catalog_items_created"] += 1
            else:
                before = snapshot(item)
                changed = False
                for field, value in item_values.items():
                    current = (
                        getattr(item, f"{field}_id") if field == "food" else getattr(item, field)
                    )
                    expected = value.pk if field == "food" else value
                    if current != expected:
                        setattr(item, field, value)
                        changed = True
                if changed:
                    item.save(update_fields=list(item_values))
                    audit(actor, item, "updated", before)
                    summary["catalog_items_updated"] += 1

        audit(
            actor,
            catalog,
            "pnae_import_completed",
            before={},
            after=summary,
            description=f"Catálogo PNAE {catalog.year} importado com {len(data['items'])} itens",
        )
        if options["json"]:
            self.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            self.stdout.write(self.style.SUCCESS(f"Importação concluída: {summary}"))
