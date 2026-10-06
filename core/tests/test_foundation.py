from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from core.fiscal_services import delete_fiscal_documents_for_reimport
from core.menu_pdf import parse_menu_pdf
from core.models import (
    AuditEvent,
    Category,
    DisposalPhoto,
    DisposalRecord,
    FiscalDocument,
    FiscalPdf,
    FiscalPnaeException,
    Food,
    MenuDocument,
    MenuMeal,
    PnaeCatalog,
    PnaeCatalogItem,
    Presentation,
    StockMovement,
    SupplierProductMapping,
    Unit,
    UnitAccess,
    UnitFood,
    User,
    WeeklyMenuItem,
)
from core.nfe import (
    NFeValidationError,
    access_key_check_digit,
    parse_nfe_xml,
    suggest_food,
    suggested_stock_quantity,
)
from core.services import audit


@override_settings(AXES_ENABLED=False)
class FoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "admin", "", "FoundationSecure!321", is_general_admin=True
        )
        cls.manager = User.objects.create_user("manager", "", "FoundationSecure!321")
        cls.operator = User.objects.create_user("operator", "", "FoundationSecure!321")
        cls.unit_a = Unit.objects.create(code="A", name="Unidade autorizada")
        cls.unit_b = Unit.objects.create(code="B", name="Unidade reservada")
        cls.manager_access = UnitAccess.objects.create(
            user=cls.manager, unit=cls.unit_a, role="manager"
        )
        UnitAccess.objects.create(user=cls.operator, unit=cls.unit_a, role="operator")
        cls.category = Category.objects.create(name="Grãos")
        cls.food = Food.objects.create(
            code="ARROZ",
            name="Arroz branco",
            category=cls.category,
            base_unit="kg",
            image_path="foods/arroz-branco.webp",
        )
        cls.unit_food = UnitFood.objects.create(unit=cls.unit_a, food=cls.food, minimum=2)
        cls.other_unit_food = UnitFood.objects.create(unit=cls.unit_b, food=cls.food, minimum=9)

    def login_as(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def test_anonymous_is_redirected(self):
        response = self.client.get(reverse("units"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/entrar/", response.url)

    def test_health_identifies_application(self):
        self.assertEqual(
            self.client.get("/health/").json(),
            {"status": "ok", "application": "food-inventory-operations"},
        )

    def test_login_case_normalization_and_hash(self):
        response = self.client.post(
            reverse("login"), {"username": "ADMIN", "password": "FoundationSecure!321"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.admin.password.startswith("argon2$"))

    def test_login_without_remember_me_expires_at_browser_close(self):
        response = self.client.post(
            reverse("login"), {"username": "admin", "password": "FoundationSecure!321"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_login_with_remember_me_persists_for_thirty_days(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "admin",
                "password": "FoundationSecure!321",
                "remember_me": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertFalse(session.get_expire_at_browser_close())
        self.assertGreater(session.get_expiry_age(), 29 * 24 * 60 * 60)
        self.assertLessEqual(session.get_expiry_age(), 30 * 24 * 60 * 60)

    def test_initial_admin_command_creates_individual_temporary_passwords(self):
        with TemporaryDirectory() as folder:
            output = f"{folder}/credenciais.txt"
            call_command(
                "criar_administradores_iniciais",
                "renata-teste",
                "aline-teste",
                output=output,
                stdout=StringIO(),
            )
            renata = User.objects.get(username="renata-teste")
            aline = User.objects.get(username="aline-teste")
            self.assertTrue(renata.is_general_admin and aline.is_general_admin)
            self.assertTrue(renata.must_change_password and aline.must_change_password)
            self.assertNotEqual(renata.password, aline.password)
            self.assertFalse(check_password("", renata.password))
            with open(output, encoding="utf-8") as stream:
                contents = stream.read()
            self.assertIn("Usuário: renata-teste", contents)
            self.assertIn("Usuário: aline-teste", contents)

    def test_login_cannot_redirect_offsite(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "admin",
                "password": "FoundationSecure!321",
                "next": "https://example.org/",
            },
        )
        self.assertEqual(response.url, reverse("dashboard"))

    def test_logout_is_post_only(self):
        self.login_as(self.admin)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)
        self.assertEqual(self.client.get(reverse("units")).status_code, 302)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            client.post(reverse("unit_create"), {"name": "Forgery", "code": "X"}).status_code, 403
        )
        self.assertFalse(Unit.objects.filter(code="X").exists())

    def test_unit_list_is_scoped(self):
        self.login_as(self.operator)
        response = self.client.get(reverse("units"))
        self.assertContains(response, self.unit_a.name)
        self.assertNotContains(response, self.unit_b.name)
        self.assertEqual(response["Cache-Control"], "no-store, private")

    def test_navigation_uses_stock_for_employee_and_keeps_catalog_for_admin(self):
        self.login_as(self.operator)
        response = self.client.get(reverse("units"))
        self.assertContains(response, "Estoque</a>")
        self.assertContains(response, "<h1>Estoque</h1>", html=True)
        self.assertNotContains(response, "Alimentos</a>")

        self.login_as(self.admin)
        response = self.client.get(reverse("units"))
        self.assertContains(response, "Unidades</a>")
        self.assertContains(response, "Alimentos</a>")
        self.assertContains(response, "Atualizações</a>")

    def test_product_updates_are_written_for_admins_only(self):
        self.login_as(self.admin)
        response = self.client.get(reverse("product_updates"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Atualizações")
        self.assertContains(response, "Novidades sem termos técnicos")
        self.assertContains(response, "Catálogo PNAE e compras mais seguras")
        self.assertContains(response, "Notas fiscais mais rápidas de conferir")
        self.assertContains(response, "Na prática:")

        self.login_as(self.operator)
        response = self.client.get(reverse("product_updates"))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(self.client.get(reverse("units")), "Atualizações</a>")

    def test_unit_stock_lists_available_items_before_zero_stock(self):
        zero_food = Food.objects.create(
            code="ZERO-FIRST",
            name="Abacaxi sem estoque",
            category=self.category,
            base_unit="kg",
        )
        available_food = Food.objects.create(
            code="AVAILABLE-LAST",
            name="Zinco com estoque",
            category=self.category,
            base_unit="kg",
        )
        UnitFood.objects.create(unit=self.unit_a, food=zero_food, quantity=0, minimum=0)
        UnitFood.objects.create(unit=self.unit_a, food=available_food, quantity=5, minimum=0)

        self.login_as(self.operator)
        response = self.client.get(reverse("unit_detail", args=[self.unit_a.pk]))
        content = response.content.decode()
        self.assertLess(
            content.index(available_food.name),
            content.index(zero_food.name),
        )

    def test_unit_list_orders_and_displays_code_before_name(self):
        Unit.objects.create(code="U10", name="Décima")
        Unit.objects.create(code="U02", name="Segunda")
        self.login_as(self.admin)
        response = self.client.get(reverse("units"))
        content = response.content.decode()
        self.assertLess(content.index("U02"), content.index("U10"))
        self.assertLess(content.index("<th>Código</th>"), content.index("<th>Unidade</th>"))

    def test_direct_unit_url_is_scoped(self):
        self.login_as(self.manager)
        self.assertEqual(
            self.client.get(reverse("unit_detail", args=[self.unit_b.pk])).status_code, 404
        )

    def test_search_cannot_leak_other_unit(self):
        self.login_as(self.operator)
        response = self.client.get(reverse("units"), {"q": "reservada"})
        self.assertNotContains(response, self.unit_b.name)

    def test_revoked_access_applies_next_request(self):
        self.login_as(self.manager)
        UnitAccess.objects.filter(pk=self.manager_access.pk).update(is_active=False)
        self.assertEqual(
            self.client.get(reverse("unit_detail", args=[self.unit_a.pk])).status_code, 404
        )

    def test_inactive_unit_is_hidden(self):
        self.login_as(self.operator)
        Unit.objects.filter(pk=self.unit_a.pk).update(is_active=False)
        self.assertEqual(
            self.client.get(reverse("unit_detail", args=[self.unit_a.pk])).status_code, 404
        )

    def test_administrator_sees_all_units(self):
        self.login_as(self.admin)
        response = self.client.get(reverse("units"))
        self.assertContains(response, self.unit_a.name)
        self.assertContains(response, self.unit_b.name)

    def test_operator_cannot_use_admin_views_even_with_post(self):
        self.login_as(self.operator)
        for name in ("unit_create", "food_create", "category_create", "user_create"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
                self.assertEqual(
                    self.client.post(reverse(name), {"is_general_admin": "on"}).status_code, 403
                )
        self.assertEqual(self.client.get(reverse("users")).status_code, 403)

    def test_operator_cannot_change_minimum(self):
        self.login_as(self.operator)
        response = self.client.post(
            reverse("unit_food_edit", args=[self.unit_a.pk, self.unit_food.pk]),
            {"food": self.food.pk, "minimum": "100"},
        )
        self.assertEqual(response.status_code, 403)

    def test_operator_sees_quantity_controls_without_management_details(self):
        self.login_as(self.operator)
        response = self.client.get(reverse("unit_detail", args=[self.unit_a.pk]))
        self.assertContains(response, "Quantidade")
        self.assertContains(response, "foods/arroz-branco.webp")
        self.assertContains(response, "Movimentar")
        self.assertContains(response, "Sem estoque")
        self.assertNotContains(response, "Editar configuração")
        self.assertNotContains(response, "mínimo 2")

    def test_operator_can_adjust_quantity_with_audit_without_going_below_zero(self):
        self.login_as(self.operator)
        url = reverse("unit_food_quantity", args=[self.unit_a.pk, self.unit_food.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"action": "increase"})
        self.assertRedirects(
            response, reverse("unit_detail", args=[self.unit_a.pk]) + f"#food-{self.unit_food.pk}"
        )
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("1"))
        event = AuditEvent.objects.latest("pk")
        self.assertEqual(event.action, "quantity_adjusted")
        self.assertEqual(event.before["quantity"], "0.000")
        self.assertEqual(event.after["quantity"], "1.000")

        self.client.post(url, {"action": "decrease"})
        audit_count = AuditEvent.objects.count()
        self.client.post(url, {"action": "decrease"})
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("0"))
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_quantity_adjustment_is_scoped_and_validated(self):
        self.login_as(self.operator)
        own_url = reverse("unit_food_quantity", args=[self.unit_a.pk, self.unit_food.pk])
        other_url = reverse("unit_food_quantity", args=[self.unit_b.pk, self.other_unit_food.pk])
        self.assertEqual(self.client.post(own_url, {"action": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(other_url, {"action": "increase"}).status_code, 404)

    def test_initial_food_import_is_idempotent_and_starts_all_quantities_at_zero(self):
        call_command("importar_alimentos_iniciais", stdout=StringIO())
        call_command("importar_alimentos_iniciais", stdout=StringIO())
        self.assertEqual(Food.objects.count(), 122)
        self.assertEqual(UnitFood.objects.count(), Unit.objects.count() * 122)
        self.assertFalse(UnitFood.objects.exclude(quantity=0).exists())
        self.assertFalse(Food.objects.filter(image_path="").exists())

    def test_manager_can_change_only_own_unit_minimum(self):
        self.login_as(self.manager)
        response = self.client.post(
            reverse("unit_food_edit", args=[self.unit_a.pk, self.unit_food.pk]),
            {"food": self.food.pk, "minimum": "2,500", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.minimum, Decimal("2.500"))
        event = AuditEvent.objects.latest("pk")
        self.assertEqual(event.unit, self.unit_a)
        self.assertEqual(event.before["minimum"], "2.000")
        self.assertEqual(event.after["minimum"], "2.500")

    def test_cannot_inject_other_unit_item_id(self):
        self.login_as(self.manager)
        response = self.client.post(
            reverse("unit_food_edit", args=[self.unit_a.pk, self.other_unit_food.pk]),
            {"food": self.food.pk, "minimum": "100"},
        )
        self.assertEqual(response.status_code, 404)
        self.other_unit_food.refresh_from_db()
        self.assertEqual(self.other_unit_food.minimum, 9)

    def test_role_differs_by_unit(self):
        UnitAccess.objects.create(user=self.manager, unit=self.unit_b, role="operator")
        self.login_as(self.manager)
        self.assertEqual(
            self.client.get(reverse("unit_detail", args=[self.unit_b.pk])).status_code, 200
        )
        self.assertEqual(
            self.client.post(
                reverse("unit_food_edit", args=[self.unit_b.pk, self.other_unit_food.pk]),
                {"minimum": "20"},
            ).status_code,
            403,
        )

    def test_account_without_units_cannot_read_catalog(self):
        orphan = User.objects.create_user("orphan")
        self.login_as(orphan)
        self.assertEqual(self.client.get(reverse("foods")).status_code, 403)

    def test_unit_creation_is_audited_and_normalizes_code(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("unit_create"), {"name": "Nova unidade", "code": "u03", "is_active": "on"}
        )
        self.assertEqual(response.status_code, 302)
        unit = Unit.objects.get(code="U03")
        event = AuditEvent.objects.get(entity="unit", entity_id=str(unit.pk))
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.before, {})
        self.assertEqual(event.after["name"], "Nova unidade")

    def test_case_insensitive_duplicate_unit_is_rejected(self):
        self.login_as(self.admin)
        response = self.client.post(reverse("unit_create"), {"name": "Duplicada", "code": "a"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Este código já pertence")
        self.assertEqual(Unit.objects.count(), 2)

    def test_duplicate_category_ignores_accents_and_spaces(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("category_create"), {"name": "  GRAOS  ", "is_active": "on"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Já existe um cadastro")
        self.assertEqual(Category.objects.count(), 1)

    def test_duplicate_food_name_is_rejected(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("food_create"),
            {
                "name": "ARROZ   BRANCO",
                "code": "OUTRO",
                "category": self.category.pk,
                "base_unit": "kg",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Já existe um cadastro")
        self.assertEqual(Food.objects.count(), 1)

    def test_base_unit_cannot_change_in_crafted_post(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("food_edit", args=[self.food.pk]),
            {
                "name": self.food.name,
                "code": self.food.code,
                "category": self.category.pk,
                "base_unit": "L",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.food.refresh_from_db()
        self.assertEqual(self.food.base_unit, "kg")

    def test_pack_accepts_exact_decimal_and_rejects_zero(self):
        self.login_as(self.admin)
        url = reverse("presentation_create", args=[self.food.pk])
        response = self.client.post(
            url, {"name": "Pacote 5 kg", "base_quantity": "5,000", "is_active": "on"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Presentation.objects.get().base_quantity, Decimal("5.000"))
        response = self.client.post(url, {"name": "Inválida", "base_quantity": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Presentation.objects.count(), 1)

    def test_fractional_count_is_rejected(self):
        food = Food.objects.create(code="OVO", name="Ovo", category=self.category, base_unit="un")
        self.login_as(self.admin)
        response = self.client.post(
            reverse("presentation_create", args=[food.pk]),
            {"name": "Caixa", "base_quantity": "1,500"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "quantidade inteira")
        self.assertFalse(Presentation.objects.exists())

    def test_manager_cannot_create_fractional_minimum_for_units(self):
        food = Food.objects.create(code="OVO", name="Ovo", category=self.category, base_unit="un")
        self.login_as(self.manager)
        response = self.client.post(
            reverse("unit_food_create", args=[self.unit_a.pk]),
            {"food": food.pk, "minimum": "1,500"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "quantidade inteira")

    def test_negative_minimum_is_rejected(self):
        self.login_as(self.manager)
        response = self.client.post(
            reverse("unit_food_edit", args=[self.unit_a.pk, self.unit_food.pk]), {"minimum": "-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.minimum, 2)

    def test_temporary_password_forces_change(self):
        self.operator.must_change_password = True
        self.operator.temporary_password_expires_at = timezone.now() + timedelta(hours=1)
        self.operator.save()
        self.login_as(self.operator)
        self.assertRedirects(self.client.get(reverse("units")), reverse("password_change"))

    def test_expired_temporary_password_logs_out(self):
        self.operator.must_change_password = True
        self.operator.temporary_password_expires_at = timezone.now() - timedelta(hours=1)
        self.operator.save()
        self.login_as(self.operator)
        self.assertEqual(self.client.get(reverse("units")).url, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_change_audits_no_secret(self):
        self.login_as(self.operator)
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "FoundationSecure!321",
                "new_password1": "ChangedCredential!654",
                "new_password2": "ChangedCredential!654",
            },
        )
        self.assertEqual(response.status_code, 302)
        event = AuditEvent.objects.get(action="password_changed")
        self.assertEqual(event.before, {})
        self.assertEqual(event.after, {})
        self.assertEqual(self.client.get(reverse("units")).status_code, 200)

    def test_password_policy_requires_at_least_eight_characters(self):
        with self.assertRaises(ValidationError):
            validate_password("Aa1!xyz", self.operator)
        validate_password("Aa1!xyzz", self.operator)

    def test_password_forms_offer_visibility_controls(self):
        self.login_as(self.admin)
        response = self.client.get(reverse("reset_password", args=[self.operator.pk]))
        self.assertContains(response, "data-password-toggle", count=2)
        self.assertContains(response, "password-visibility.js")

        self.login_as(self.operator)
        response = self.client.get(reverse("password_change"))
        self.assertContains(response, "data-password-toggle", count=3)
        self.assertContains(response, "Use pelo menos 8 caracteres")

    def test_admin_cannot_disable_self_or_remove_own_role(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("user_edit", args=[self.admin.pk]), {"username": "admin"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "próprio acesso")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active and self.admin.is_general_admin)

    def test_disabled_and_reactivated_account_cannot_reuse_session(self):
        employee = Client()
        employee.force_login(self.operator, backend="django.contrib.auth.backends.ModelBackend")
        self.login_as(self.admin)
        url = reverse("user_edit", args=[self.operator.pk])
        self.assertEqual(self.client.post(url, {"username": "operator"}).status_code, 302)
        self.assertEqual(
            self.client.post(url, {"username": "operator", "is_active": "on"}).status_code, 302
        )
        self.assertEqual(employee.get(reverse("units")).status_code, 302)

    def test_changed_membership_revokes_existing_sessions(self):
        employee = Client()
        employee.force_login(self.manager, backend="django.contrib.auth.backends.ModelBackend")
        self.login_as(self.admin)
        self.client.post(
            reverse("access_edit", args=[self.manager.pk, self.manager_access.pk]),
            {"unit": self.unit_a.pk, "role": "operator", "is_active": "on"},
        )
        self.assertEqual(employee.get(reverse("units")).status_code, 302)

    def test_admin_can_create_user_with_forced_password_change(self):
        self.login_as(self.admin)
        response = self.client.post(
            reverse("user_create"),
            {
                "username": "NEWPERSON",
                "first_name": "Pessoa",
                "password1": "TemporarySecret!753",
                "password2": "TemporarySecret!753",
                "is_staff": "on",
                "is_superuser": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newperson")
        self.assertTrue(user.must_change_password)
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertGreater(user.temporary_password_expires_at, timezone.now())
        event = AuditEvent.objects.get(entity="user", entity_id=str(user.pk))
        self.assertNotIn("password", event.after)

    def test_manager_audit_is_scoped_and_hides_memberships(self):
        own = audit(self.admin, self.unit_food, "updated", unit=self.unit_a)
        other = audit(self.admin, self.other_unit_food, "updated", unit=self.unit_b)
        access_event = audit(self.admin, self.manager_access, "updated", unit=self.unit_a)
        self.login_as(self.manager)
        self.assertEqual(self.client.get(reverse("audit_detail", args=[own.pk])).status_code, 200)
        for event in (other, access_event):
            self.assertEqual(
                self.client.get(reverse("audit_detail", args=[event.pk])).status_code, 404
            )

    def test_audit_filters_can_be_combined_and_keep_scope(self):
        own = audit(self.manager, self.unit_food, "updated", unit=self.unit_a)
        audit(self.admin, self.other_unit_food, "updated", unit=self.unit_b)
        self.login_as(self.admin)
        today = timezone.localdate().isoformat()
        response = self.client.get(
            reverse("audit"),
            {
                "q": self.unit_food.food.name,
                "actor": self.manager.pk,
                "unit": self.unit_a.pk,
                "action": "updated",
                "entity": "unitfood",
                "date_from": today,
                "date_to": today,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, own.description)
        self.assertContains(response, "1 registro encontrado")
        self.assertContains(response, "Data inicial")
        self.assertEqual([event.pk for event in response.context["page"]], [own.pk])

        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.get(reverse("audit"), {"date_from": tomorrow})
        self.assertContains(response, "Nenhuma alteração encontrada")

    def test_audit_is_read_only_at_model_and_database(self):
        event = audit(self.admin, self.unit_a, "created")
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(description="changed")
        for statement in (
            "UPDATE core_auditevent SET description = 'changed' WHERE id = %s",
            "DELETE FROM core_auditevent WHERE id = %s",
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, [event.pk])
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).description, self.unit_a.name)

    def test_save_rolls_back_if_audit_fails(self):
        self.login_as(self.admin)
        with patch("core.views.audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("unit_create"), {"name": "Rollback", "code": "ROLLBACK"})
        self.assertFalse(Unit.objects.filter(code="ROLLBACK").exists())

    def test_ref_documents_not_served(self):
        self.assertEqual(self.client.get("/private-data/sample.xlsx").status_code, 404)


@override_settings(AXES_ENABLED=False)
class PnaeCatalogImportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "pnae-admin", "", "FoundationSecure!321", is_general_admin=True
        )
        self.employee = User.objects.create_user("pnae-employee", "", "FoundationSecure!321")
        self.unit_a = Unit.objects.create(code="P01", name="Primeira unidade")
        self.unit_b = Unit.objects.create(code="P02", name="Segunda unidade")
        category = Category.objects.create(name="Hortifruti")
        self.abacaxi = Food.objects.create(
            code="AL001",
            name="Abacaxi",
            category=category,
            base_unit=Food.Measure.UNIT,
        )
        UnitFood.objects.create(unit=self.unit_a, food=self.abacaxi, minimum=0, quantity=0)

    def test_import_is_complete_safe_and_idempotent(self):
        first_output = StringIO()
        call_command("importar_catalogo_pnae", "--json", stdout=first_output)

        catalog = PnaeCatalog.objects.get(year=2026, is_active=True)
        self.abacaxi.refresh_from_db()
        self.assertEqual(catalog.items.count(), 112)
        self.assertEqual(catalog.items.values("food_id").distinct().count(), 112)
        self.assertEqual(self.abacaxi.base_unit, Food.Measure.KG)
        self.assertEqual(Food.objects.count(), 112)
        self.assertEqual(UnitFood.objects.count(), 224)
        self.assertTrue(all(item.food_id for item in catalog.items.all()))

        counts = (
            PnaeCatalog.objects.count(),
            PnaeCatalogItem.objects.count(),
            Food.objects.count(),
            UnitFood.objects.count(),
            Presentation.objects.count(),
        )
        second_output = StringIO()
        call_command("importar_catalogo_pnae", "--json", stdout=second_output)
        self.assertEqual(
            counts,
            (
                PnaeCatalog.objects.count(),
                PnaeCatalogItem.objects.count(),
                Food.objects.count(),
                UnitFood.objects.count(),
                Presentation.objects.count(),
            ),
        )
        self.assertIn('"foods_created": 0', second_output.getvalue())

    def test_only_administrators_can_open_catalog(self):
        call_command("importar_catalogo_pnae", stdout=StringIO())
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("pnae_catalog"), {"q": "8915.13.031-44"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "112")
        self.assertContains(response, "8915.13.031-44")

        self.client.force_login(self.employee, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("pnae_catalog")).status_code, 403)


@override_settings(AXES_ENABLED=False)
class FiscalDocumentTests(TestCase):
    ACCESS_KEY = "33260906222792000125550010016714361192233207"

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "fiscal-admin", "", "FoundationSecure!321", is_general_admin=True
        )
        cls.manager = User.objects.create_user("fiscal-manager", "", "FoundationSecure!321")
        cls.operator = User.objects.create_user("fiscal-operator", "", "FoundationSecure!321")
        cls.unit_a = Unit.objects.create(
            code="U01", name="Central Kitchen", fiscal_address_match="Sample Street"
        )
        cls.unit_b = Unit.objects.create(code="U02", name="Outra unidade")
        UnitAccess.objects.create(user=cls.manager, unit=cls.unit_a, role=UnitAccess.Role.MANAGER)
        UnitAccess.objects.create(user=cls.operator, unit=cls.unit_a, role=UnitAccess.Role.OPERATOR)
        cls.category = Category.objects.create(name="Cereais")
        cls.food = Food.objects.create(
            code="AL001",
            name="Arroz branco",
            category=cls.category,
            base_unit=Food.Measure.KG,
        )
        cls.unit_food = UnitFood.objects.create(
            unit=cls.unit_a, food=cls.food, minimum=0, quantity=0
        )

    @classmethod
    def xml(cls, *, status="100"):
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe><infNFe Id="NFe{cls.ACCESS_KEY}" versao="4.00">
    <ide><mod>55</mod><serie>1</serie><nNF>1671436</nNF><dhEmi>2026-09-21T11:17:00-03:00</dhEmi><dPrevEntrega>2026-09-22</dPrevEntrega></ide>
    <emit><CNPJ>06222792000125</CNPJ><xNome>Nova Coqueiro Alimentos</xNome></emit>
    <dest><CNPJ>00000000000100</CNPJ><xNome>FOODOPS</xNome><enderDest><xLgr>Sample Street</xLgr><nro>73</nro><xBairro>Central District</xBairro><xMun>Sample City</xMun><UF>RJ</UF></enderDest></dest>
    <det nItem="1"><prod><cProd>100</cProd><xProd>ARROZ BRANCO KG</xProd><NCM>10063021</NCM><CFOP>5102</CFOP><uCom>KG</uCom><qCom>8.0000</qCom><vUnCom>14.7900000000</vUnCom><vProd>118.32</vProd></prod></det>
    <total><ICMSTot><vProd>118.32</vProd><vNF>118.32</vNF></ICMSTot></total>
    <pag><detPag><tPag>17</tPag><vPag>118.32</vPag></detPag></pag>
  </infNFe></NFe>
  <protNFe versao="4.00"><infProt><tpAmb>1</tpAmb><chNFe>{cls.ACCESS_KEY}</chNFe><dhRecbto>2026-09-21T11:18:00-03:00</dhRecbto><nProt>333260123456789</nProt><cStat>{status}</cStat><xMotivo>Autorizado o uso da NF-e</xMotivo></infProt></protNFe>
</nfeProc>""".encode()

    @classmethod
    def pdf(cls, *, embedded_xml=False):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(
            f"BT /F1 10 Tf 40 700 Td (DANFE CHAVE DE ACESSO {cls.ACCESS_KEY}) Tj ET".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(content)
        if embedded_xml:
            writer.add_attachment("nota.xml", cls.xml())
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    def upload(self, user=None):
        self.client.force_login(
            user or self.admin, backend="django.contrib.auth.backends.ModelBackend"
        )
        return self.client.post(
            reverse("fiscal_document_upload"),
            {
                "xml_files": SimpleUploadedFile(
                    "nota.xml", self.xml(), content_type="application/xml"
                )
            },
        )

    def upload_pdf(self, *, user=None, embedded_xml=False):
        self.client.force_login(
            user or self.admin, backend="django.contrib.auth.backends.ModelBackend"
        )
        return self.client.post(
            reverse("fiscal_pdf_upload"),
            {
                "pdf_files": SimpleUploadedFile(
                    "danfe.pdf",
                    self.pdf(embedded_xml=embedded_xml),
                    content_type="application/pdf",
                )
            },
        )

    def confirmation_data(self, document, unit=None):
        item = document.items.get()
        return {
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "500",
            "items-0-item_id": item.pk,
            "items-0-food": self.food.pk,
            "items-0-stock_quantity": "8.000",
            f"items-0-unit_{(unit or self.unit_a).pk}": "8.000",
            "action": "confirm",
        }

    @classmethod
    def alternate_xml(cls):
        base = f"{cls.ACCESS_KEY[:-9]}87654321"
        access_key = f"{base}{access_key_check_digit(base + '0')}"
        return cls.xml().replace(cls.ACCESS_KEY.encode(), access_key.encode())

    def test_parser_rejects_unsafe_or_unauthorized_xml(self):
        malicious = b"""<!DOCTYPE nfeProc [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">&xxe;</nfeProc>"""
        with self.assertRaises(NFeValidationError):
            parse_nfe_xml(malicious)
        with self.assertRaisesMessage(NFeValidationError, "não está autorizada"):
            parse_nfe_xml(self.xml(status="110"))

    def test_upload_suggests_unit_food_and_quantity(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        document = FiscalDocument.objects.get()
        item = document.items.get()
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)
        self.assertEqual(document.unit, self.unit_a)
        self.assertEqual(item.food, self.food)
        self.assertEqual(item.stock_quantity, Decimal("8.000"))
        self.assertEqual(document.xml_content, self.xml())
        self.assertTrue(
            AuditEvent.objects.filter(entity="fiscaldocument", action="xml_uploaded").exists()
        )

    def test_duplicate_xml_does_not_create_or_add_twice(self):
        first = self.upload()
        second = self.upload()
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(FiscalDocument.objects.count(), 1)

        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("8.000"))

    def test_nfe_can_be_completely_deleted_and_uploaded_again_with_audit(self):
        self.upload()
        document = FiscalDocument.objects.get()
        original_id = document.pk
        original_hash = document.xml_sha256
        SupplierProductMapping.objects.create(
            supplier_cnpj=document.supplier_cnpj,
            supplier_code="100",
            food=self.food,
        )
        self.upload_pdf()
        document.refresh_from_db()
        document.status = FiscalDocument.Status.IMPORTED
        document.save(update_fields=["status"])

        summaries = delete_fiscal_documents_for_reimport([document.pk], self.admin)

        self.assertEqual(summaries[0]["number"], "1671436")
        self.assertEqual(summaries[0]["items"], 1)
        self.assertEqual(summaries[0]["pdfs"], 1)
        self.assertFalse(FiscalDocument.objects.exists())
        self.assertFalse(FiscalPdf.objects.exists())
        self.assertEqual(SupplierProductMapping.objects.count(), 1)
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("0.000"))

        event = AuditEvent.objects.get(action="fiscal_document_deleted")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.entity_id, str(original_id))
        self.assertEqual(event.before["document"]["xml_sha256"], original_hash)
        self.assertEqual(len(event.before["items"]), 1)
        self.assertEqual(len(event.before["pdfs"]), 1)
        self.assertNotIn("xml_content", event.before["document"])
        self.assertEqual(event.after, {})

        response = self.upload()
        self.assertEqual(FiscalDocument.objects.count(), 1)
        reuploaded = FiscalDocument.objects.get()
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[reuploaded.pk]))
        self.assertNotEqual(reuploaded.pk, original_id)

    def test_nfe_deletion_is_blocked_when_it_has_stock_movements(self):
        self.upload()
        document = FiscalDocument.objects.get()
        self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            self.confirmation_data(document),
        )

        with self.assertRaisesMessage(ValidationError, "movimentações de estoque"):
            delete_fiscal_documents_for_reimport([document.pk], self.admin)

        self.assertTrue(FiscalDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="fiscal_document_deleted").exists())

    def test_batch_upload_receives_multiple_xmls(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(
            reverse("fiscal_document_upload"),
            {
                "xml_files": [
                    SimpleUploadedFile("nota-1.xml", self.xml(), content_type="application/xml"),
                    SimpleUploadedFile(
                        "nota-2.xml", self.alternate_xml(), content_type="application/xml"
                    ),
                ]
            },
        )
        self.assertRedirects(response, reverse("fiscal_documents"))
        self.assertEqual(FiscalDocument.objects.count(), 2)

    def test_draft_saves_partial_distribution_without_changing_stock(self):
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data["action"] = "save_draft"
        data[f"items-0-unit_{self.unit_a.pk}"] = "3.000"
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        document.refresh_from_db()
        self.unit_food.refresh_from_db()
        draft = document.review_draft["items"][str(document.items.get().pk)]
        self.assertEqual(draft["allocations"][str(self.unit_a.pk)], "3.000")
        self.assertEqual(self.unit_food.quantity, Decimal("0.000"))
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)

    def test_saved_draft_can_be_deleted_with_complete_audit_log(self):
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data["action"] = "save_draft"
        data[f"items-0-unit_{self.unit_a.pk}"] = "3.000"
        self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        document.refresh_from_db()
        deleted_content = document.review_draft

        response = self.client.get(reverse("fiscal_document_detail", args=[document.pk]))
        self.assertContains(response, "Excluir rascunho")
        response = self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            {"action": "delete_draft"},
        )

        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        document.refresh_from_db()
        self.unit_food.refresh_from_db()
        self.assertEqual(document.review_draft, {})
        self.assertIsNone(document.draft_saved_by)
        self.assertIsNone(document.draft_saved_at)
        self.assertEqual(self.unit_food.quantity, Decimal("0.000"))
        self.assertFalse(StockMovement.objects.exists())

        event = AuditEvent.objects.get(action="fiscal_draft_deleted")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.before["review_draft"], deleted_content)
        self.assertEqual(event.before["draft_saved_by_name"], self.admin.display_name)
        self.assertEqual(event.after["review_draft"], {})
        detail = self.client.get(reverse("audit_detail", args=[event.pk]))
        self.assertContains(detail, "Conteúdo do rascunho")
        self.assertContains(detail, "3.000")

        self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            {"action": "delete_draft"},
        )
        self.assertEqual(AuditEvent.objects.filter(action="fiscal_draft_deleted").count(), 1)

    def test_confirmation_distributes_one_invoice_between_units(self):
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data[f"items-0-unit_{self.unit_a.pk}"] = "3.000"
        data[f"items-0-unit_{self.unit_b.pk}"] = "5.000"
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        document.refresh_from_db()
        self.unit_food.refresh_from_db()
        unit_b_food = UnitFood.objects.get(unit=self.unit_b, food=self.food)
        self.assertEqual(self.unit_food.quantity, Decimal("3.000"))
        self.assertEqual(unit_b_food.quantity, Decimal("5.000"))
        self.assertEqual(document.stock_movements.count(), 2)
        self.assertIsNone(document.unit)
        self.assertEqual(document.items.get().stock_movement_items.count(), 2)

    def test_outside_pnae_item_requires_justification_and_admin_approval(self):
        catalog = PnaeCatalog.objects.create(
            year=2026,
            title="PNAE 2026",
            reference="Referência de teste",
            source_name="catalogo.pdf",
            is_active=True,
        )
        PnaeCatalogItem.objects.create(
            catalog=catalog,
            code="8915.11.016-06",
            name="ARROZ POLIDO",
            acquisition_unit="kg",
            source_page=1,
            food=self.food,
        )
        outside = Food.objects.create(
            code="AL999",
            name="Açúcar refinado",
            category=self.category,
            base_unit=Food.Measure.KG,
        )
        UnitFood.objects.create(unit=self.unit_a, food=outside, minimum=0, quantity=0)
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data["items-0-food"] = outside.pk
        data["items-0-pnae_exception_reason"] = "Compra autorizada para atividade especial"
        data["action"] = "save_draft"
        self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)

        exception = FiscalPnaeException.objects.get()
        self.assertEqual(exception.status, FiscalPnaeException.Status.PENDING)
        self.assertEqual(exception.food, outside)

        data["action"] = "confirm"
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aprovação administrativa PNAE pendente")
        document.refresh_from_db()
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)

        data["action"] = "approve_pnae_exceptions"
        self.client.force_login(self.manager, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertEqual(response.status_code, 403)
        exception.refresh_from_db()
        self.assertEqual(exception.status, FiscalPnaeException.Status.PENDING)

        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        exception.refresh_from_db()
        self.assertEqual(exception.status, FiscalPnaeException.Status.APPROVED)
        self.assertEqual(exception.reviewed_by, self.admin)

        data["action"] = "confirm"
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        document.refresh_from_db()
        self.assertEqual(document.status, FiscalDocument.Status.IMPORTED)
        self.assertTrue(AuditEvent.objects.filter(action="pnae_exception_approved").exists())

    def test_distribution_above_total_is_rejected(self):
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data[f"items-0-unit_{self.unit_a.pk}"] = "9.000"
        response = self.client.post(reverse("fiscal_document_detail", args=[document.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ultrapassa o total")
        self.unit_food.refresh_from_db()
        self.assertEqual(self.unit_food.quantity, Decimal("0.000"))

    def test_admin_can_create_missing_food_during_review(self):
        self.upload()
        document = FiscalDocument.objects.get()
        item = document.items.get()
        response = self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            {
                "action": "create_food",
                "new_food-item_id": item.pk,
                "new_food-name": "Arroz especial da nota",
                "new_food-category": self.category.pk,
                "new_food-base_unit": Food.Measure.KG,
            },
        )
        self.assertRedirects(response, reverse("fiscal_document_detail", args=[document.pk]))
        created = Food.objects.get(name="Arroz especial da nota")
        item.refresh_from_db()
        self.assertEqual(item.food, created)
        self.assertEqual(
            SupplierProductMapping.objects.get(
                supplier_cnpj=document.supplier_cnpj, supplier_code=item.supplier_code
            ).food,
            created,
        )

    def test_description_matching_works_for_packaging_units(self):
        sugar = Food.objects.create(
            code="AL999",
            name="Açúcar refinado",
            category=self.category,
            base_unit=Food.Measure.KG,
        )
        self.assertEqual(suggest_food("ACUCAR REFINADO FD", "FD", [self.food, sugar]), sugar)
        packaged = SimpleNamespace(
            description="ACUCAR REFINADO 1KG",
            commercial_unit="UN",
            quantity=Decimal("90"),
        )
        self.assertEqual(suggested_stock_quantity(packaged, sugar), Decimal("90.000"))

    def test_food_detail_shows_normalized_price_history_to_fiscal_users(self):
        self.upload()
        document = FiscalDocument.objects.get()
        response = self.client.get(reverse("fiscal_document_detail", args=[document.pk]))
        self.assertContains(response, "Ver histórico de preços")

        response = self.client.get(reverse("food_detail", args=[self.food.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Histórico de preços")
        self.assertContains(response, "Evolução do preço por kg")
        self.assertContains(response, "NF-e 1671436")
        self.assertEqual(response.context["price_history"]["latest_price"], Decimal("14.7900"))
        self.assertEqual(len(response.context["price_history"]["chart"]["points"]), 1)

        self.client.force_login(self.manager, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("food_detail", args=[self.food.pk]))
        self.assertContains(response, "Histórico de preços")

        self.client.force_login(self.operator, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("food_detail", args=[self.food.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Histórico de preços")

    def test_opening_pending_document_refreshes_missing_suggestions(self):
        self.upload()
        document = FiscalDocument.objects.get()
        item = document.items.get()
        item.food = None
        item.stock_quantity = None
        item.save(update_fields=["food", "stock_quantity"])
        response = self.client.get(reverse("fiscal_document_detail", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.food, self.food)
        self.assertEqual(item.stock_quantity, Decimal("8.000"))
        self.assertTrue(AuditEvent.objects.filter(action="fiscal_suggestions_refreshed").exists())

    def test_pdf_waits_for_xml_then_links_to_received_nfe(self):
        response = self.upload_pdf()
        self.assertRedirects(response, reverse("fiscal_documents"))
        fiscal_pdf = FiscalPdf.objects.get()
        self.assertEqual(fiscal_pdf.status, FiscalPdf.Status.AWAITING_XML)
        self.assertIsNone(fiscal_pdf.document_id)
        self.assertEqual(fiscal_pdf.access_key, self.ACCESS_KEY)
        self.assertTrue(
            AuditEvent.objects.filter(entity="fiscalpdf", action="pdf_uploaded").exists()
        )

        listing = self.client.get(reverse("fiscal_documents"))
        self.assertContains(listing, "PDFs aguardando XML")
        self.assertContains(listing, "danfe.pdf")
        self.assertEqual(
            self.client.get(reverse("dashboard")).context["pending_documents_count"], 1
        )
        download = self.client.get(reverse("fiscal_pdf_download", args=[fiscal_pdf.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "application/pdf")

        self.upload()
        fiscal_pdf.refresh_from_db()
        self.assertEqual(fiscal_pdf.status, FiscalPdf.Status.LINKED)
        self.assertEqual(fiscal_pdf.document, FiscalDocument.objects.get())
        detail = self.client.get(reverse("fiscal_document_detail", args=[fiscal_pdf.document_id]))
        self.assertContains(detail, "Baixar PDF")

    def test_pdf_with_embedded_xml_starts_nfe_review(self):
        response = self.upload_pdf(embedded_xml=True)
        document = FiscalDocument.objects.get()
        fiscal_pdf = FiscalPdf.objects.get()
        self.assertRedirects(
            response,
            reverse("fiscal_document_detail", args=[document.pk]),
        )
        self.assertEqual(fiscal_pdf.document, document)
        self.assertEqual(fiscal_pdf.status, FiscalPdf.Status.LINKED)
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)

    def test_pdf_upload_rejects_non_pdf_content(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(
            reverse("fiscal_pdf_upload"),
            {
                "pdf_files": SimpleUploadedFile(
                    "danfe.pdf", b"not a pdf", content_type="application/pdf"
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "não é um PDF válido")
        self.assertFalse(FiscalPdf.objects.exists())

    def test_confirmation_updates_stock_mapping_and_audit_atomically(self):
        self.upload()
        document = FiscalDocument.objects.get()
        data = self.confirmation_data(document)
        data["items-0-lot_code"] = "LOTE-NFE-01"
        data["items-0-expires_on"] = "2027-04-30"
        response = self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            data,
        )
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()
        self.unit_food.refresh_from_db()
        self.assertEqual(document.status, FiscalDocument.Status.IMPORTED)
        self.assertEqual(document.confirmed_by, self.admin)
        self.assertEqual(self.unit_food.quantity, Decimal("8.000"))
        self.assertEqual(
            SupplierProductMapping.objects.get(
                supplier_cnpj=document.supplier_cnpj, supplier_code="100"
            ).food,
            self.food,
        )
        movement = StockMovement.objects.get(fiscal_document=document)
        self.assertEqual(movement.kind, StockMovement.Kind.RECEIPT)
        movement_item = movement.items.get()
        self.assertEqual(movement_item.lot_code, "LOTE-NFE-01")
        self.assertEqual(movement_item.expires_on.isoformat(), "2027-04-30")
        self.assertTrue(
            AuditEvent.objects.filter(
                action="movement_created", unit=self.unit_a, entity_id=str(movement.pk)
            ).exists()
        )
        self.assertTrue(AuditEvent.objects.filter(action="fiscal_imported").exists())

    def test_confirmation_rolls_back_when_audit_fails(self):
        self.upload()
        document = FiscalDocument.objects.get()
        with patch("core.fiscal_services.audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("fiscal_document_detail", args=[document.pk]),
                    self.confirmation_data(document),
                )
        document.refresh_from_db()
        self.unit_food.refresh_from_db()
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)
        self.assertEqual(self.unit_food.quantity, Decimal("0.000"))
        self.assertFalse(SupplierProductMapping.objects.exists())

    def test_manager_cannot_import_into_another_unit(self):
        self.upload(self.manager)
        document = FiscalDocument.objects.get()
        response = self.client.post(
            reverse("fiscal_document_detail", args=[document.pk]),
            self.confirmation_data(document, self.unit_b),
        )
        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.status, FiscalDocument.Status.REVIEW)
        self.assertContains(response, "Distribua este item para pelo menos uma unidade")

    def test_operator_cannot_access_fiscal_documents(self):
        self.client.force_login(self.operator, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("fiscal_documents")).status_code, 403)
        self.assertEqual(self.client.get(reverse("fiscal_document_upload")).status_code, 403)
        self.assertEqual(self.client.get(reverse("fiscal_pdf_upload")).status_code, 403)

    def test_authorized_user_can_download_original_xml(self):
        self.upload(self.manager)
        document = FiscalDocument.objects.get()
        response = self.client.get(reverse("fiscal_document_download", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.xml())
        self.assertEqual(response["Content-Type"], "application/xml")


@override_settings(AXES_ENABLED=False)
class MenuDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "menu-admin", "", "FoundationSecure!321", is_general_admin=True
        )
        cls.employee = User.objects.create_user("menu-employee", "", "FoundationSecure!321")

    @classmethod
    def pdf(cls):
        writer = PdfWriter()
        page = writer.add_blank_page(width=842, height=595)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        x_positions = [30, 162, 294, 426, 558, 690, 822]
        y_positions = [470, 410, 350, 290, 230, 170, 110]
        operations = ["0.7 w"]
        operations.extend(f"{x} 110 m {x} 470 l S" for x in x_positions)
        operations.extend(f"30 {y} m 822 {y} l S" for y in y_positions)
        operations.append(
            "BT /F1 12 Tf 30 540 Td (Cardapio Semanal) Tj "
            "0 -18 Td (Semana 05 a 09 de Janeiro de 2026) Tj "
            "0 -18 Td (Nutricionista Teste - CRN 12345) Tj ET"
        )
        rows = [
            ["Horarios", "Segunda", "Terca", "Quarta", "Quinta", "Sexta"],
            ["Colacao", "Banana", "Maca", "Manga", "Goiaba", "Melancia"],
            ["Colacao Bercario", "Banana", "Maca", "Manga", "Goiaba", "Melancia"],
            ["Almoco", "Arroz", "Feijao", "Frango", "Peixe", "Ovos"],
            ["Refeicao Saida", "Bolo", "Mingau", "Canja", "Frutas", "Sopa"],
            ["Sobremesa", "Laranja", "Maca", "Melancia", "Abacaxi", "Banana"],
        ]
        for row_index, row in enumerate(rows):
            y = 445 - row_index * 60
            for column_index, value in enumerate(row):
                x = x_positions[column_index] + 5
                operations.append(f"BT /F1 8 Tf {x} {y} Td ({value}) Tj ET")
        content = DecodedStreamObject()
        content.set_data("\n".join(operations).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    def upload(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        return self.client.post(
            reverse("menu_document_upload"),
            {
                "menu_files": SimpleUploadedFile(
                    "Cardapio 05 a 09 de Janeiro de 2026.pdf",
                    self.pdf(),
                    content_type="application/pdf",
                )
            },
        )

    def test_parser_extracts_week_and_twenty_five_references(self):
        parsed = parse_menu_pdf(self.pdf(), "Cardapio 05 a 09 de Janeiro de 2026.pdf")
        self.assertEqual(str(parsed.week_start), "2026-01-05")
        self.assertEqual(str(parsed.week_end), "2026-01-09")
        self.assertEqual(len(parsed.meals), 25)
        self.assertEqual(parsed.nutritionist_name, "Nutricionista Teste")
        self.assertFalse(parsed.warnings)

    def test_admin_upload_is_audited_and_duplicate_is_not_created(self):
        response = self.upload()
        document = MenuDocument.objects.get()
        self.assertRedirects(response, reverse("menu_document_detail", args=[document.pk]))
        self.assertEqual(document.meals.count(), 25)
        self.assertEqual(document.status, MenuDocument.Status.PROCESSED)
        self.assertTrue(
            AuditEvent.objects.filter(action="menu_uploaded", entity_id=str(document.pk)).exists()
        )

        response = self.upload()
        self.assertRedirects(response, reverse("menu_document_detail", args=[document.pk]))
        self.assertEqual(MenuDocument.objects.count(), 1)
        self.assertEqual(MenuMeal.objects.count(), 25)

    def test_authenticated_employee_can_view_but_only_admin_can_upload(self):
        self.upload()
        document = MenuDocument.objects.get()
        self.client.force_login(self.employee, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("menu_documents"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cardápio")
        self.assertContains(response, "Almoço")
        self.assertNotContains(response, "＋ Enviar cardápio")
        self.assertEqual(
            self.client.get(reverse("menu_document_detail", args=[document.pk])).status_code, 200
        )
        download = self.client.get(reverse("menu_document_download", args=[document.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, self.pdf())
        self.assertEqual(self.client.get(reverse("menu_document_upload")).status_code, 403)

    def test_weekly_planner_starts_blank_and_lists_pdf_options_by_meal(self):
        self.upload()
        response = self.client.get(reverse("menu_documents"), {"semana": "2026-09-28"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Planejamento semanal")
        self.assertContains(response, "28/09/2026 a 02/10/2026")
        self.assertContains(response, "＋ Adicionar prato", count=25)
        self.assertContains(response, "Opções de pratos")
        self.assertContains(response, "Banana")
        self.assertContains(response, "Almoço")
        self.assertFalse(WeeklyMenuItem.objects.exists())

    def test_admin_can_add_change_and_remove_weekly_dish_with_audit(self):
        self.upload()
        snacks = list(MenuMeal.objects.filter(meal_type=MenuMeal.Type.SNACK).order_by("pk")[:2])
        response = self.client.post(
            reverse("weekly_menu_item_save"),
            {
                "week_start": "2026-09-28",
                "service_date": "2026-09-29",
                "meal_type": MenuMeal.Type.SNACK,
                "source_meal": snacks[0].pk,
            },
        )
        self.assertEqual(
            response.url,
            reverse("menu_documents") + "?semana=2026-09-28#planejamento",
        )
        item = WeeklyMenuItem.objects.get()
        self.assertEqual(item.description, snacks[0].description)
        self.assertTrue(
            AuditEvent.objects.filter(action="menu_item_created", entity_id=str(item.pk)).exists()
        )

        self.client.post(
            reverse("weekly_menu_item_save"),
            {
                "week_start": "2026-09-28",
                "service_date": "2026-09-29",
                "meal_type": MenuMeal.Type.SNACK,
                "source_meal": snacks[1].pk,
            },
        )
        item.refresh_from_db()
        self.assertEqual(item.description, snacks[1].description)
        self.assertTrue(
            AuditEvent.objects.filter(action="menu_item_updated", entity_id=str(item.pk)).exists()
        )

        self.client.post(reverse("weekly_menu_item_delete", args=[item.pk]))
        self.assertFalse(WeeklyMenuItem.objects.exists())
        self.assertTrue(
            AuditEvent.objects.filter(action="menu_item_deleted", entity_id=str(item.pk)).exists()
        )

    def test_employee_can_view_plan_but_cannot_change_it(self):
        self.upload()
        meal = MenuMeal.objects.filter(meal_type=MenuMeal.Type.SNACK).first()
        self.client.force_login(self.employee, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("menu_documents"), {"semana": "2026-09-28"})
        self.assertContains(response, "Planejamento semanal")
        self.assertNotContains(response, "＋ Adicionar prato")
        self.assertEqual(
            self.client.post(
                reverse("weekly_menu_item_save"),
                {
                    "service_date": "2026-09-29",
                    "meal_type": MenuMeal.Type.SNACK,
                    "source_meal": meal.pk,
                },
            ).status_code,
            403,
        )
        self.assertFalse(WeeklyMenuItem.objects.exists())

    def test_anonymous_user_is_redirected(self):
        self.assertEqual(self.client.get(reverse("menu_documents")).status_code, 302)


@override_settings(AXES_ENABLED=False)
class DisposalRecordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "disposal-admin", "", "FoundationSecure!321", is_general_admin=True
        )
        cls.employee = User.objects.create_user("disposal-employee", "", "FoundationSecure!321")
        cls.other_employee = User.objects.create_user("disposal-other", "", "FoundationSecure!321")
        cls.unit = Unit.objects.create(code="D01", name="Unidade do descarte")
        cls.other_unit = Unit.objects.create(code="D02", name="Outra unidade")
        UnitAccess.objects.create(user=cls.employee, unit=cls.unit, role=UnitAccess.Role.OPERATOR)
        UnitAccess.objects.create(
            user=cls.other_employee, unit=cls.other_unit, role=UnitAccess.Role.OPERATOR
        )

    @staticmethod
    def photo(name="descarte.jpg", color=(210, 40, 30)):
        output = BytesIO()
        Image.new("RGB", (320, 240), color).save(output, "JPEG", quality=95)
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/jpeg")

    def create(self, user=None, unit=None, photos=None):
        self.client.force_login(
            user or self.employee, backend="django.contrib.auth.backends.ModelBackend"
        )
        return self.client.post(
            reverse("disposal_record_create"),
            {
                "unit": (unit or self.unit).pk,
                "occurred_on": "2026-09-29",
                "observation": "Embalagens danificadas durante o recebimento.",
                "photos": photos
                or [
                    self.photo(),
                    self.photo("segunda-foto.png", color=(20, 130, 80)),
                ],
            },
        )

    def test_employee_creates_record_with_multiple_sanitized_photos_and_audit(self):
        response = self.create()
        record = DisposalRecord.objects.get()
        self.assertRedirects(response, reverse("disposal_record_detail", args=[record.pk]))
        self.assertEqual(record.created_by, self.employee)
        self.assertEqual(record.photos.count(), 2)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="disposal_created", entity_id=str(record.pk), unit=self.unit
            ).exists()
        )
        for photo in record.photos.all():
            self.assertEqual(photo.content_type, "image/jpeg")
            self.assertEqual((photo.width, photo.height), (320, 240))
            self.assertEqual(photo.size, len(photo.image_content))

        response = self.client.get(reverse("disposal_records"))
        self.assertContains(response, "Embalagens danificadas")
        photo = record.photos.first()
        response = self.client.get(reverse("disposal_photo", args=[record.pk, photo.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_employee_cannot_create_or_view_record_for_another_unit(self):
        response = self.create(unit=self.other_unit)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Faça uma escolha válida")
        self.assertFalse(DisposalRecord.objects.exists())

        self.create(user=self.other_employee, unit=self.other_unit, photos=[self.photo()])
        record = DisposalRecord.objects.get()
        photo = record.photos.get()
        self.client.force_login(self.employee, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.get(reverse("disposal_record_detail", args=[record.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("disposal_photo", args=[record.pk, photo.pk])).status_code,
            404,
        )

    def test_invalid_image_is_rejected_without_partial_record(self):
        invalid = SimpleUploadedFile("falsa.jpg", b"not an image", content_type="image/jpeg")
        response = self.create(photos=[invalid])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Não foi possível validar esta foto")
        self.assertFalse(DisposalRecord.objects.exists())
        self.assertFalse(DisposalPhoto.objects.exists())

    def test_anonymous_user_is_redirected(self):
        self.assertEqual(self.client.get(reverse("disposal_records")).status_code, 302)
        self.assertEqual(self.client.get(reverse("disposal_record_create")).status_code, 302)

    def test_new_record_starts_with_today_selected(self):
        self.client.force_login(self.employee, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("disposal_record_create"))
        self.assertEqual(response.context["form"]["occurred_on"].value(), timezone.localdate())
        self.assertContains(response, f'value="{timezone.localdate().isoformat()}"')


class LoginRateLimitTests(TestCase):
    def test_failed_logins_lock_out(self):
        User.objects.create_user("limited", "", "ActualCredential!991")
        for _ in range(5):
            response = self.client.post(
                reverse("login"), {"username": "limited", "password": "wrong"}
            )
        self.assertEqual(response.status_code, 429)
        response = self.client.post(
            reverse("login"), {"username": "limited", "password": "ActualCredential!991"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)
