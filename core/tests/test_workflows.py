from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import (
    AuditEvent,
    Category,
    Food,
    StockMovement,
    StockMovementItem,
    Unit,
    UnitAccess,
    UnitFood,
    User,
)
from core.stock_services import create_stock_movement


@override_settings(AXES_ENABLED=False)
class InventoryWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "workflow-admin", "", "FoundationSecure!321", is_general_admin=True
        )
        cls.manager = User.objects.create_user("workflow-manager", "", "FoundationSecure!321")
        cls.operator = User.objects.create_user("workflow-operator", "", "FoundationSecure!321")
        cls.unit = Unit.objects.create(code="U01", name="Unidade principal")
        cls.other_unit = Unit.objects.create(code="U02", name="Unidade reservada")
        UnitAccess.objects.create(user=cls.manager, unit=cls.unit, role=UnitAccess.Role.MANAGER)
        UnitAccess.objects.create(user=cls.operator, unit=cls.unit, role=UnitAccess.Role.OPERATOR)
        cls.category = Category.objects.create(name="Mercearia")
        cls.food = Food.objects.create(
            code="FEIJAO",
            name="Feijão",
            category=cls.category,
            base_unit=Food.Measure.KG,
            image_path="foods/feijao-preto.webp",
        )
        cls.counted_food = Food.objects.create(
            code="OVO",
            name="Ovo",
            category=cls.category,
            base_unit=Food.Measure.UNIT,
        )
        cls.unit_food = UnitFood.objects.create(
            unit=cls.unit, food=cls.food, minimum=Decimal("3"), quantity=Decimal("0")
        )
        UnitFood.objects.create(
            unit=cls.unit,
            food=cls.counted_food,
            minimum=Decimal("6"),
            quantity=Decimal("0"),
        )
        cls.other_unit_food = UnitFood.objects.create(
            unit=cls.other_unit, food=cls.food, minimum=Decimal("8"), quantity=Decimal("0")
        )

    def login_as(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def movement_data(self, **overrides):
        data = {
            "unit": self.unit.pk,
            "kind": StockMovement.Kind.RECEIPT,
            "food": self.food.pk,
            "quantity": "5.500",
            "occurred_at": "2026-09-28T10:30",
            "lot_code": "LT-2026-09",
            "expires_on": "2027-03-15",
            "reason": "Recebimento manual",
        }
        data.update(overrides)
        return data

    def test_zero_stock_photo_is_marked_and_positive_stock_is_not(self):
        self.login_as(self.operator)
        url = reverse("unit_detail", args=[self.unit.pk])
        response = self.client.get(url)
        self.assertContains(response, "out-of-stock")
        self.assertContains(response, "Sem estoque", count=2)

        self.client.post(reverse("stock_movement_create"), self.movement_data())
        response = self.client.get(url, {"q": "Feijão"})
        self.assertNotContains(response, "Sem estoque")
        self.assertNotContains(response, "out-of-stock")

    def test_movement_updates_balance_and_records_optional_lot_and_expiry(self):
        self.login_as(self.operator)
        response = self.client.post(reverse("stock_movement_create"), self.movement_data())
        movement = StockMovement.objects.get()
        item = movement.items.get()
        self.assertRedirects(response, reverse("stock_movement_detail", args=[movement.pk]))
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("5.500"))
        self.assertEqual(item.delta, Decimal("5.500"))
        self.assertEqual(item.lot_code, "LT-2026-09")
        self.assertEqual(item.expires_on.isoformat(), "2027-03-15")
        self.assertTrue(AuditEvent.objects.filter(entity="stockmovement").exists())

        response = self.client.post(
            reverse("stock_movement_create"),
            self.movement_data(
                kind=StockMovement.Kind.CONSUMPTION,
                quantity="6.000",
                lot_code="",
                expires_on="",
                reason="Consumo",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saldo insuficiente")
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("5.500"))

    def test_counted_food_rejects_fraction_and_operator_cannot_adjust(self):
        self.login_as(self.operator)
        response = self.client.post(
            reverse("stock_movement_create"),
            self.movement_data(food=self.counted_food.pk, quantity="1.500"),
        )
        self.assertContains(response, "quantidade inteira")
        response = self.client.post(
            reverse("stock_movement_create"),
            self.movement_data(kind=StockMovement.Kind.ADJUSTMENT_IN, reason="Contagem"),
        )
        self.assertContains(response, "Faça uma escolha válida")
        self.assertFalse(StockMovement.objects.exists())

    def test_reversal_preserves_original_and_ledger_is_immutable(self):
        movement = create_stock_movement(
            unit=self.unit,
            kind=StockMovement.Kind.RECEIPT,
            lines=[{"food": self.food, "quantity": Decimal("4")}],
            actor=self.manager,
            reason="Entrada",
        )
        self.login_as(self.manager)
        response = self.client.post(
            reverse("stock_movement_reverse", args=[movement.pk]),
            {"reason": "Lançamento duplicado"},
        )
        reversal = StockMovement.objects.get(reversed_movement=movement)
        self.assertRedirects(response, reverse("stock_movement_detail", args=[reversal.pk]))
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("0"))
        self.assertEqual(reversal.items.get().delta, Decimal("-4"))

        response = self.client.post(
            reverse("stock_movement_reverse", args=[movement.pk]),
            {"reason": "Tentativa repetida"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "já foi estornada")
        with self.assertRaises(ValidationError):
            StockMovement.objects.filter(pk=movement.pk).update(reason="alterado")
        with self.assertRaises(ValidationError):
            StockMovementItem.objects.filter(movement=movement).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE core_stockmovement SET reason = %s WHERE id = %s",
                    ["alterado", movement.pk],
                )

    def test_dashboard_and_movements_are_scoped_by_unit(self):
        create_stock_movement(
            unit=self.other_unit,
            kind=StockMovement.Kind.RECEIPT,
            lines=[{"food": self.food, "quantity": Decimal("2")}],
            actor=self.admin,
            reason="Outra unidade",
        )
        self.login_as(self.operator)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["units_count"], 1)
        self.assertEqual(response.context["zero_stock_count"], 2)
        self.assertEqual(len(response.context["recent_movements"]), 0)
        alerts = self.client.get(reverse("stock_alerts"), {"status": "zero"})
        self.assertTrue(all(row.unit_id == self.unit.pk for row in alerts.context["page"]))
        other = StockMovement.objects.get(unit=self.other_unit)
        self.assertEqual(
            self.client.get(reverse("stock_movement_detail", args=[other.pk])).status_code, 404
        )

    def test_admin_employee_preview_is_scoped_and_read_only(self):
        self.login_as(self.admin)
        response = self.client.post(reverse("employee_preview"), {"unit": self.unit.pk})
        self.assertRedirects(response, reverse("dashboard"))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Visualização como funcionário")
        self.assertContains(response, "somente leitura")
        self.assertEqual(response.context["units_count"], 1)
        self.assertEqual(self.client.get(reverse("users")).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("stock_movement_create"), self.movement_data()).status_code,
            403,
        )
        self.assertEqual(self.client.post(reverse("employee_preview_exit")).status_code, 302)
        self.assertEqual(self.client.get(reverse("users")).status_code, 200)


@override_settings(AXES_ENABLED=False)
class RegistrationApprovalTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "approval-admin", "", "FoundationSecure!321", is_general_admin=True
        )

    def registration_data(self, **overrides):
        data = {
            "username": "Nova.Pessoa",
            "first_name": "Nova",
            "last_name": "Pessoa",
            "email": "nova@example.org",
            "password1": "PendingAccount!753",
            "password2": "PendingAccount!753",
        }
        data.update(overrides)
        return data

    def test_registration_waits_for_admin_approval_before_login(self):
        response = self.client.post(reverse("register"), self.registration_data())
        self.assertRedirects(response, reverse("registration_submitted"))
        person = User.objects.get(username="nova.pessoa")
        self.assertFalse(person.is_active)
        self.assertEqual(person.registration_status, User.RegistrationStatus.PENDING)
        self.assertFalse(self.client.login(username="nova.pessoa", password="PendingAccount!753"))
        self.assertTrue(
            AuditEvent.objects.filter(action="registration_requested", actor=None).exists()
        )

        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(reverse("approve_user", args=[person.pk]))
        self.assertRedirects(response, reverse("user_detail", args=[person.pk]))
        person.refresh_from_db()
        self.assertTrue(person.is_active)
        self.assertEqual(person.registration_status, User.RegistrationStatus.APPROVED)

        browser = Client()
        response = browser.post(
            reverse("login"),
            {"username": "nova.pessoa", "password": "PendingAccount!753"},
        )
        self.assertRedirects(response, reverse("dashboard"))

    def test_admin_can_reject_and_later_approve_registration(self):
        self.client.post(reverse("register"), self.registration_data())
        person = User.objects.get(username="nova.pessoa")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.client.post(reverse("reject_user", args=[person.pk]))
        person.refresh_from_db()
        self.assertFalse(person.is_active)
        self.assertEqual(person.registration_status, User.RegistrationStatus.REJECTED)
        response = self.client.post(
            reverse("user_edit", args=[person.pk]),
            {"username": person.username, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use a ação Aprovar cadastro")
        self.client.post(reverse("approve_user", args=[person.pk]))
        person.refresh_from_db()
        self.assertTrue(person.is_active)
        self.assertEqual(person.registration_status, User.RegistrationStatus.APPROVED)

    def test_rejection_endpoint_cannot_disable_an_approved_account(self):
        approved = User.objects.create_user("already-approved", "", "ApprovedAccount!753")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(reverse("reject_user", args=[approved.pk]))
        self.assertEqual(response.status_code, 403)
        approved.refresh_from_db()
        self.assertTrue(approved.is_active)
        self.assertEqual(approved.registration_status, User.RegistrationStatus.APPROVED)
