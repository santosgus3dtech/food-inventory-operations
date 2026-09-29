from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import Category, Food, Unit, UnitFood, normalize_name
from core.services import audit

CATALOG = [
    ("Hortifruti", "kg", "Abacate"),
    ("Hortifruti", "un", "Abacaxi"),
    ("Hortifruti", "kg", "Abobrinha"),
    ("Hortifruti", "kg", "Abóbora"),
    ("Mercearia", "un", "Achocolatado"),
    ("Hortifruti", "un", "Agrião"),
    ("Hortifruti", "kg", "Aipim"),
    ("Hortifruti", "un", "Alface"),
    ("Hortifruti", "kg", "Alho"),
    ("Mercearia", "un", "Amido de Milho"),
    ("Mercearia", "kg", "Arroz Branco"),
    ("Mercearia", "kg", "Arroz Integral"),
    ("Mercearia", "un", "Atum em lata"),
    ("Mercearia", "un", "Aveia 200g"),
    ("Hortifruti", "un", "Açafrão em pó"),
    ("Mercearia", "kg", "Açúcar"),
    ("Hortifruti", "kg", "Banana"),
    ("Hortifruti", "kg", "Batata"),
    ("Hortifruti", "kg", "Batata Doce"),
    ("Mercearia", "un", "Batata Palha"),
    ("Hortifruti", "kg", "Beringela"),
    ("Hortifruti", "kg", "Beterraba"),
    ("Mercearia", "un", "Biscoito Cream Cracker"),
    ("Mercearia", "un", "Biscoito Maisena"),
    ("Mercearia", "un", "Biscoito Rosquinha"),
    ("Mercearia", "un", "Biscoito de Leite"),
    ("Hortifruti", "un", "Brócolis"),
    ("Mercearia", "un", "Café 500g"),
    ("Mercearia", "un", "Canela em Pó"),
    ("Mercearia", "un", "Canjica 500g"),
    ("Hortifruti", "kg", "Caqui"),
    ("Proteínas", "kg", "Carne Bovina"),
    ("Proteínas", "kg", "Carne Moída"),
    ("Mercearia", "un", "Catchup"),
    ("Hortifruti", "kg", "Cebola"),
    ("Hortifruti", "kg", "Cenoura"),
    ("Mercearia", "un", "Champignon"),
    ("Hortifruti", "un", "Cheiro Verde"),
    ("Mercearia", "un", "Chocolate em pó 70%"),
    ("Hortifruti", "kg", "Chuchu"),
    ("Mercearia", "un", "Coco Ralado 100g"),
    ("Hortifruti", "un", "Coentro"),
    ("Hortifruti", "un", "Couve"),
    ("Hortifruti", "un", "Couve-Flor"),
    ("Mercearia", "un", "Cravo 30g"),
    ("Mercearia", "un", "Creme de Leite"),
    ("Mercearia", "un", "Creme de Ricota"),
    ("Mercearia", "un", "Danone"),
    ("Mercearia", "un", "Doce de Leite"),
    ("Mercearia", "un", "Ervilha em lata"),
    ("Mercearia", "un", "Ervilha para Sopa"),
    ("Hortifruti", "un", "Espinafre"),
    ("Mercearia", "un", "Extrato de Tomate 300g"),
    ("Mercearia", "kg", "Farinha de Mandioca"),
    ("Mercearia", "kg", "Farinha de Trigo"),
    ("Mercearia", "kg", "Farinha de Trigo Integral"),
    ("Mercearia", "kg", "Feijão Carioca"),
    ("Mercearia", "kg", "Feijão Preto"),
    ("Mercearia", "un", "Fermento de Pó Químico"),
    ("Proteínas", "kg", "File de Peito de Frango"),
    ("Proteínas", "kg", "File de Peixe"),
    ("Hortifruti", "un", "Folha de Louro"),
    ("Mercearia", "kg", "Fubá"),
    ("Proteínas", "kg", "Fígado"),
    ("Hortifruti", "kg", "Goiaba"),
    ("Mercearia", "un", "Goiabada"),
    ("Mercearia", "un", "Guaraná Natural 1L"),
    ("Hortifruti", "kg", "Inhame"),
    ("Hortifruti", "kg", "Laranja"),
    ("Mercearia", "un", "Leite de Coco 500ml"),
    ("Mercearia", "un", "Leite em Pó Instantâneo 400g"),
    ("Mercearia", "un", "Lentilha"),
    ("Hortifruti", "kg", "Limão"),
    ("Mercearia", "kg", "Macarrão Espaguete"),
    ("Mercearia", "un", "Macarrão Farfalle"),
    ("Mercearia", "kg", "Macarrão Parafuso"),
    ("Hortifruti", "un", "Mamão Formosa"),
    ("Hortifruti", "kg", "Manga"),
    ("Mercearia", "un", "Manteiga 200g"),
    ("Hortifruti", "kg", "Maracujá"),
    ("Hortifruti", "kg", "Maçã"),
    ("Hortifruti", "un", "Melancia"),
    ("Hortifruti", "un", "Melão"),
    ("Mercearia", "un", "Milho"),
    ("Mercearia", "un", "Milho de Pipoca 400g"),
    ("Proteínas", "kg", "Moela"),
    ("Hortifruti", "kg", "Morango"),
    ("Mercearia", "un", "Mostarda 180g"),
    ("Hortifruti", "un", "Orégano 200g"),
    ("Mercearia", "un", "Ovo"),
    ("Hortifruti", "kg", "Pepino"),
    ("Hortifruti", "kg", "Pera"),
    ("Hortifruti", "kg", "Pimentão"),
    ("Polpas", "kg", "Polpa de Abacaxi"),
    ("Polpas", "kg", "Polpa de Abacaxi c/Hortelã"),
    ("Polpas", "kg", "Polpa de Acerola"),
    ("Polpas", "kg", "Polpa de Açaí"),
    ("Polpas", "kg", "Polpa de Caju"),
    ("Polpas", "kg", "Polpa de Cupuaçu"),
    ("Polpas", "kg", "Polpa de Goiaba"),
    ("Polpas", "kg", "Polpa de Laranja"),
    ("Polpas", "kg", "Polpa de Limão"),
    ("Polpas", "kg", "Polpa de Manga"),
    ("Polpas", "kg", "Polpa de Maracujá"),
    ("Polpas", "kg", "Polpa de Morango"),
    ("Polpas", "kg", "Polpa de Uva"),
    ("Mercearia", "un", "Presunto 500g"),
    ("Mercearia", "un", "Pão de Forma"),
    ("Mercearia", "un", "Pão de Forma Integral"),
    ("Mercearia", "un", "Pão de Milho"),
    ("Mercearia", "un", "Queijo Mussarela 500g"),
    ("Mercearia", "un", "Queijo Prato 500g"),
    ("Hortifruti", "kg", "Quiabo"),
    ("Hortifruti", "un", "Repolho"),
    ("Mercearia", "un", "Requeijão 200g"),
    ("Mercearia", "kg", "Sal"),
    ("Hortifruti", "kg", "Tangerina"),
    ("Hortifruti", "kg", "Tomate"),
    ("Hortifruti", "un", "Uva"),
    ("Hortifruti", "un", "Vagem"),
    ("Mercearia", "un", "Vinagre"),
    ("Mercearia", "un", "Óleo"),
]


class Command(BaseCommand):
    help = "Importa o catálogo inicial de alimentos e o vincula a todas as unidades."

    @transaction.atomic
    def handle(self, *args, **options):
        categories = {}
        created_categories = 0
        created_foods = 0
        updated_photos = 0
        created_links = 0

        for category_name in sorted({row[0] for row in CATALOG}):
            category = Category.objects.filter(
                normalized_name=normalize_name(category_name)
            ).first()
            if category is None:
                category = Category(name=category_name, is_active=True)
                category.full_clean()
                category.save()
                audit(None, category, "created")
                created_categories += 1
            categories[category_name] = category

        units = list(Unit.objects.all())
        for index, (category_name, base_unit, name) in enumerate(CATALOG, start=1):
            code = f"AL{index:03d}"
            image_path = f"foods/{slugify(name)}.webp"
            food = Food.objects.filter(normalized_name=normalize_name(name)).first()
            if food is None:
                if Food.objects.filter(code=code).exists():
                    raise CommandError(f"O código {code} já pertence a outro alimento.")
                food = Food(
                    code=code,
                    name=name,
                    category=categories[category_name],
                    base_unit=base_unit,
                    requires_expiry=True,
                    image_path=image_path,
                    is_active=True,
                )
                food.full_clean()
                food.save()
                audit(None, food, "created")
                created_foods += 1
            else:
                if food.base_unit != base_unit:
                    raise CommandError(
                        f"{name} já existe com medida {food.base_unit}, esperada {base_unit}."
                    )
                if food.image_path != image_path:
                    before = {
                        "code": food.code,
                        "name": food.name,
                        "category_id": food.category_id,
                        "base_unit": food.base_unit,
                        "requires_expiry": food.requires_expiry,
                        "image_path": food.image_path,
                        "is_active": food.is_active,
                    }
                    food.image_path = image_path
                    food.save(update_fields=["image_path"])
                    audit(None, food, "updated", before)
                    updated_photos += 1

            for unit in units:
                link, created = UnitFood.objects.get_or_create(
                    unit=unit,
                    food=food,
                    defaults={
                        "minimum": Decimal("0"),
                        "quantity": Decimal("0"),
                        "is_active": True,
                    },
                )
                if created:
                    audit(None, link, "created", unit=unit)
                    created_links += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Importação concluída: "
                f"{created_categories} categorias, {created_foods} alimentos, "
                f"{updated_photos} fotos atualizadas e {created_links} vínculos criados."
            )
        )
