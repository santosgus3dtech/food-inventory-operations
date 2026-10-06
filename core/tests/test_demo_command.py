from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.models import Food, StockMovement, Unit, UnitFood, User


@override_settings(DEBUG=True)
class PortfolioDemoCommandTests(TestCase):
    def setUp(self):
        User.objects.create_user(
            username="demo-admin",
            password="local-test-only",
            is_general_admin=True,
        )

    def test_command_creates_auditable_synthetic_scenario(self):
        output = StringIO()
        call_command("carregar_demo_portfolio", stdout=output)
        self.assertEqual(Unit.objects.count(), 3)
        self.assertEqual(Food.objects.count(), 5)
        self.assertEqual(StockMovement.objects.count(), 6)
        self.assertEqual(UnitFood.objects.count(), 15)
        self.assertTrue(all(row.quantity >= 0 for row in UnitFood.objects.all()))
        self.assertIn("Cenário fictício criado", output.getvalue())

    def test_command_refuses_to_mix_with_existing_units(self):
        Unit.objects.create(code="EXISTENTE", name="Unidade já existente")
        with self.assertRaisesMessage(CommandError, "Já existem unidades"):
            call_command("carregar_demo_portfolio")
