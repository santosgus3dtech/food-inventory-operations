import unicodedata
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.crypto import salted_hmac


def normalize_name(value):
    value = " ".join(value.split()).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


class User(AbstractUser):
    class RegistrationStatus(models.TextChoices):
        APPROVED = "approved", "Aprovado"
        PENDING = "pending", "Aguardando aprovação"
        REJECTED = "rejected", "Recusado"

    is_general_admin = models.BooleanField("Administrador geral", default=False)
    registration_status = models.CharField(
        "Situação do cadastro",
        max_length=12,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.APPROVED,
    )
    must_change_password = models.BooleanField(default=False)
    temporary_password_expires_at = models.DateTimeField(null=True, blank=True)
    session_version = models.PositiveIntegerField(default=0)

    @property
    def is_administrator(self):
        return self.has_administrator_role and not hasattr(self, "_employee_preview_unit_id")

    @property
    def has_administrator_role(self):
        return self.is_active and (self.is_general_admin or self.is_superuser)

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    def _get_session_auth_hash(self, secret=None):
        return salted_hmac(
            "core.User.session",
            f"{self.password}:{self.session_version}",
            secret=secret,
            algorithm="sha256",
        ).hexdigest()

    def save(self, *args, **kwargs):
        self.username = self.username.strip().lower()
        super().save(*args, **kwargs)


class Unit(models.Model):
    code = models.CharField(
        "Código",
        max_length=24,
        unique=True,
        validators=[
            RegexValidator(r"^[A-Za-z0-9_-]+$", "Use letras, números, hífen ou sublinhado.")
        ],
    )
    name = models.CharField("Nome da unidade", max_length=120)
    fiscal_address_match = models.CharField(
        "Referência do endereço na NF-e",
        max_length=160,
        blank=True,
        help_text="Trecho usado para reconhecer automaticamente a unidade, por exemplo Sample Street.",
    )
    is_active = models.BooleanField("Unidade ativa", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code", "pk"]

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        self.name = " ".join(self.name.split())
        self.fiscal_address_match = " ".join(self.fiscal_address_match.split())
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class UnitAccess(models.Model):
    class Role(models.TextChoices):
        MANAGER = "manager", "Responsável"
        OPERATOR = "operator", "Funcionário"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT)
    role = models.CharField("Perfil", max_length=16, choices=Role.choices)
    is_active = models.BooleanField("Acesso ativo", default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "unit"], name="unique_user_unit")]
        ordering = ["unit__name"]


class NamedCatalog(models.Model):
    name = models.CharField("Nome", max_length=120)
    normalized_name = models.CharField(max_length=160, unique=True, editable=False)
    is_active = models.BooleanField("Ativo", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name", "pk"]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.normalized_name = normalize_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Category(NamedCatalog):
    class Meta(NamedCatalog.Meta):
        verbose_name = "Categoria"


class Food(NamedCatalog):
    class Measure(models.TextChoices):
        KG = "kg", "Quilograma (kg)"
        LITRE = "L", "Litro (L)"
        UNIT = "un", "Unidade (un)"

    code = models.CharField(
        "Código",
        max_length=24,
        unique=True,
        validators=[
            RegexValidator(r"^[A-Za-z0-9_-]+$", "Use letras, números, hífen ou sublinhado.")
        ],
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT, verbose_name="Categoria")
    base_unit = models.CharField("Unidade de medida", max_length=2, choices=Measure.choices)
    requires_expiry = models.BooleanField("Exigir validade nos recebimentos", default=True)
    image_path = models.CharField("Foto", max_length=200, blank=True, editable=False)

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class Presentation(models.Model):
    food = models.ForeignKey(Food, on_delete=models.PROTECT, related_name="presentations")
    name = models.CharField("Nome da embalagem", max_length=100)
    normalized_name = models.CharField(max_length=140, editable=False)
    base_quantity = models.DecimalField(
        "Conteúdo na unidade de medida do alimento",
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    is_active = models.BooleanField("Embalagem ativa", default=True)

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["food", "normalized_name"], name="unique_food_pack"),
            models.CheckConstraint(condition=models.Q(base_quantity__gt=0), name="positive_pack"),
        ]

    def clean(self):
        if self.base_quantity and self.food_id and self.food.base_unit == Food.Measure.UNIT:
            if self.base_quantity != self.base_quantity.to_integral_value():
                raise ValidationError(
                    {"base_quantity": "Informe uma quantidade inteira de unidades."}
                )

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.normalized_name = normalize_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.food.name}: {self.name}"


class PnaeCatalog(models.Model):
    year = models.PositiveSmallIntegerField("Ano", unique=True)
    title = models.CharField("Título", max_length=200)
    reference = models.CharField("Referência", max_length=300)
    source_name = models.CharField("Documento de origem", max_length=180)
    source_url = models.URLField("Fonte para consulta", blank=True)
    is_active = models.BooleanField("Catálogo vigente", default=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="single_active_pnae_catalog",
            )
        ]

    def __str__(self):
        return f"PNAE {self.year}"


class PnaeCatalogItem(models.Model):
    class AcquisitionUnit(models.TextChoices):
        KG = "kg", "Quilograma (kg)"
        UNIT = "un", "Unidade (un)"

    catalog = models.ForeignKey(PnaeCatalog, on_delete=models.PROTECT, related_name="items")
    code = models.CharField("Código do item", max_length=20)
    name = models.CharField("Nome no catálogo", max_length=180)
    specification = models.TextField("Especificação", blank=True)
    acquisition_unit = models.CharField(
        "Unidade de aquisição", max_length=2, choices=AcquisitionUnit.choices
    )
    packaging = models.TextField("Embalagem ou apresentação", blank=True)
    source_page = models.PositiveSmallIntegerField("Página de origem")
    food = models.ForeignKey(
        Food,
        on_delete=models.PROTECT,
        related_name="pnae_catalog_items",
        verbose_name="Alimento no estoque",
    )
    is_active = models.BooleanField("Item permitido", default=True)

    class Meta:
        ordering = ["code", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["catalog", "code"], name="unique_pnae_catalog_code")
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class UnitFood(models.Model):
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT)
    food = models.ForeignKey(Food, on_delete=models.PROTECT)
    minimum = models.DecimalField(
        "Estoque mínimo",
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0"))],
    )
    quantity = models.DecimalField(
        "Quantidade atual",
        max_digits=12,
        decimal_places=3,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    is_active = models.BooleanField("Acompanhar nesta unidade", default=True)

    class Meta:
        ordering = ["food__name"]
        constraints = [
            models.UniqueConstraint(fields=["unit", "food"], name="unique_unit_food"),
            models.CheckConstraint(condition=models.Q(minimum__gte=0), name="nonnegative_minimum"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0), name="nonnegative_quantity"
            ),
        ]

    def clean(self):
        if self.food_id and self.food.base_unit == Food.Measure.UNIT:
            errors = {}
            if self.minimum is not None and self.minimum != self.minimum.to_integral_value():
                errors["minimum"] = "Informe uma quantidade inteira de unidades."
            if self.quantity is not None and self.quantity != self.quantity.to_integral_value():
                errors["quantity"] = "A quantidade deve ser um número inteiro de unidades."
            if errors:
                raise ValidationError(errors)

    def __str__(self):
        return f"{self.unit.name}: {self.food.name}"


class FiscalDocument(models.Model):
    class Status(models.TextChoices):
        REVIEW = "review", "Aguardando conferência"
        IMPORTED = "imported", "Entrada confirmada"

    access_key = models.CharField(
        "Chave de acesso",
        max_length=44,
        unique=True,
        validators=[RegexValidator(r"^\d{44}$", "A chave deve conter 44 números.")],
    )
    number = models.CharField("Número", max_length=20)
    series = models.CharField("Série", max_length=10)
    issued_at = models.DateTimeField("Emissão")
    delivery_date = models.DateField("Entrega prevista", null=True, blank=True)
    protocol = models.CharField("Protocolo de autorização", max_length=30)
    authorization_received_at = models.DateTimeField("Autorização", null=True, blank=True)
    supplier_cnpj = models.CharField("CNPJ do emitente", max_length=14)
    supplier_name = models.CharField("Emitente", max_length=180)
    recipient_cnpj = models.CharField("CNPJ do destinatário", max_length=14)
    recipient_name = models.CharField("Destinatário", max_length=180)
    delivery_address = models.CharField("Endereço de entrega", max_length=300)
    total = models.DecimalField("Total da NF-e", max_digits=14, decimal_places=2)
    payments = models.JSONField("Pagamentos", default=list, editable=False)
    validation_warnings = models.JSONField("Avisos de validação", default=list, editable=False)
    signature_present = models.BooleanField("Assinatura presente", default=False, editable=False)
    xml_sha256 = models.CharField(max_length=64, unique=True, editable=False)
    xml_content = models.BinaryField(editable=False)
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fiscal_documents",
        verbose_name="Unidade",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REVIEW)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_fiscal_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="confirmed_fiscal_documents",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    review_draft = models.JSONField("Rascunho da conferência", default=dict, blank=True)
    draft_saved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fiscal_document_drafts",
    )
    draft_saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at", "-pk"]

    def __str__(self):
        return f"NF-e {self.number} — {self.supplier_name}"


class FiscalPdf(models.Model):
    class Status(models.TextChoices):
        AWAITING_XML = "awaiting_xml", "Aguardando XML"
        LINKED = "linked", "Vinculado à NF-e"

    access_key = models.CharField(
        "Chave de acesso",
        max_length=44,
        db_index=True,
        validators=[RegexValidator(r"^\d{44}$", "A chave deve conter 44 números.")],
    )
    original_name = models.CharField("Nome do arquivo", max_length=180)
    page_count = models.PositiveSmallIntegerField("Páginas")
    pdf_sha256 = models.CharField(max_length=64, unique=True, editable=False)
    pdf_content = models.BinaryField(editable=False)
    validation_warnings = models.JSONField("Avisos de validação", default=list, editable=False)
    document = models.ForeignKey(
        FiscalDocument,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pdfs",
        verbose_name="NF-e",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.AWAITING_XML,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_fiscal_pdfs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status="awaiting_xml", document__isnull=True)
                    | models.Q(status="linked", document__isnull=False)
                ),
                name="fiscal_pdf_status_matches_document",
            )
        ]

    def __str__(self):
        return f"DANFE {self.access_key}"


class FiscalDocumentItem(models.Model):
    document = models.ForeignKey(FiscalDocument, on_delete=models.PROTECT, related_name="items")
    line_number = models.PositiveIntegerField("Item")
    supplier_code = models.CharField("Código no fornecedor", max_length=60)
    ean = models.CharField("EAN", max_length=20, blank=True)
    description = models.CharField("Descrição na NF-e", max_length=240)
    ncm = models.CharField("NCM", max_length=10, blank=True)
    cfop = models.CharField("CFOP", max_length=6, blank=True)
    commercial_unit = models.CharField("Unidade na NF-e", max_length=12)
    quantity = models.DecimalField("Quantidade na NF-e", max_digits=14, decimal_places=4)
    unit_price = models.DecimalField("Preço unitário", max_digits=16, decimal_places=10)
    line_total = models.DecimalField("Total do item", max_digits=14, decimal_places=2)
    food = models.ForeignKey(
        Food,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fiscal_document_items",
        verbose_name="Alimento",
    )
    stock_quantity = models.DecimalField(
        "Quantidade para o estoque",
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    lot_code = models.CharField("Lote", max_length=80, blank=True)
    expires_on = models.DateField("Validade", null=True, blank=True)

    class Meta:
        ordering = ["line_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "line_number"], name="unique_fiscal_document_line"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="positive_fiscal_item_quantity"
            ),
            models.CheckConstraint(
                condition=models.Q(line_total__gte=0), name="nonnegative_fiscal_item_total"
            ),
            models.CheckConstraint(
                condition=models.Q(stock_quantity__isnull=True) | models.Q(stock_quantity__gt=0),
                name="positive_fiscal_stock_quantity",
            ),
        ]

    def __str__(self):
        return f"{self.document.number}/{self.line_number}: {self.description}"


class FiscalPnaeException(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Aguardando aprovação"
        APPROVED = "approved", "Aprovada"
        REJECTED = "rejected", "Recusada"

    item = models.OneToOneField(
        FiscalDocumentItem,
        on_delete=models.PROTECT,
        related_name="pnae_exception",
        verbose_name="Item da NF-e",
    )
    catalog = models.ForeignKey(PnaeCatalog, on_delete=models.PROTECT)
    food = models.ForeignKey(Food, on_delete=models.PROTECT)
    reason = models.CharField("Justificativa", max_length=500)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_pnae_exceptions",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_pnae_exceptions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-pk"]

    def __str__(self):
        return f"Exceção PNAE: {self.item}"


class SupplierProductMapping(models.Model):
    supplier_cnpj = models.CharField("CNPJ do emitente", max_length=14)
    supplier_code = models.CharField("Código no fornecedor", max_length=60)
    ean = models.CharField("EAN", max_length=20, blank=True)
    food = models.ForeignKey(Food, on_delete=models.PROTECT, related_name="supplier_mappings")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplier_cnpj", "supplier_code"], name="unique_supplier_product"
            )
        ]

    def __str__(self):
        return f"{self.supplier_cnpj}/{self.supplier_code}: {self.food.name}"


class ImmutableLedgerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Movimentações confirmadas não podem ser alteradas.")

    def delete(self):
        raise ValidationError("Movimentações confirmadas não podem ser apagadas.")


class StockMovement(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", "Entrada"
        CONSUMPTION = "consumption", "Consumo"
        LOSS = "loss", "Perda"
        SUPPLIER_RETURN = "supplier_return", "Devolução ao fornecedor"
        STOCK_RETURN = "stock_return", "Retorno ao estoque"
        ADJUSTMENT_IN = "adjustment_in", "Ajuste de entrada"
        ADJUSTMENT_OUT = "adjustment_out", "Ajuste de saída"
        OPENING = "opening", "Saldo inicial"
        REVERSAL = "reversal", "Estorno"

    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="stock_movements")
    kind = models.CharField("Tipo", max_length=24, choices=Kind.choices)
    occurred_at = models.DateTimeField("Data e hora", default=timezone.now)
    reason = models.CharField("Motivo ou observação", max_length=240, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="stock_movements_created",
    )
    fiscal_document = models.ForeignKey(
        FiscalDocument,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    reversed_movement = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableLedgerQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [models.Index(fields=["unit", "occurred_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Movimentações confirmadas não podem ser alteradas.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Movimentações confirmadas não podem ser apagadas.")

    def __str__(self):
        return f"{self.get_kind_display()} em {self.unit.code}"


class StockMovementItem(models.Model):
    movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, related_name="items")
    unit_food = models.ForeignKey(UnitFood, on_delete=models.PROTECT, related_name="movement_items")
    delta = models.DecimalField("Alteração do saldo", max_digits=12, decimal_places=3)
    lot_code = models.CharField("Lote", max_length=80, blank=True)
    expires_on = models.DateField("Validade", null=True, blank=True)
    fiscal_document_item = models.ForeignKey(
        FiscalDocumentItem,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movement_items",
    )

    objects = ImmutableLedgerQuerySet.as_manager()

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(delta=0), name="nonzero_stock_delta")
        ]

    @property
    def quantity(self):
        return abs(self.delta)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Itens de movimentação não podem ser alterados.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Itens de movimentação não podem ser apagados.")

    def __str__(self):
        return f"{self.unit_food.food.name}: {self.delta}"


class MenuDocument(models.Model):
    class Status(models.TextChoices):
        PROCESSED = "processed", "Processado"
        NEEDS_REVIEW = "needs_review", "Requer conferência"

    original_name = models.CharField("Nome do arquivo", max_length=180)
    title = models.CharField("Título", max_length=180, default="Cardápio semanal")
    week_start = models.DateField("Início da semana", null=True, blank=True)
    week_end = models.DateField("Fim da semana", null=True, blank=True)
    nutritionist_name = models.CharField("Nutricionista", max_length=120, blank=True)
    nutritionist_registration = models.CharField("Registro profissional", max_length=40, blank=True)
    notes = models.TextField("Observações", blank=True)
    page_count = models.PositiveSmallIntegerField("Páginas")
    pdf_sha256 = models.CharField(max_length=64, unique=True, editable=False)
    pdf_content = models.BinaryField(editable=False)
    source_text = models.TextField(editable=False, blank=True)
    warnings = models.JSONField("Avisos de processamento", default=list, editable=False)
    status = models.CharField(
        "Situação", max_length=16, choices=Status.choices, default=Status.PROCESSED
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_menu_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-week_start", "-created_at", "-pk"]

    def __str__(self):
        if self.week_start and self.week_end:
            return f"Cardápio de {self.week_start:%d/%m/%Y} a {self.week_end:%d/%m/%Y}"
        return self.original_name


class MenuMeal(models.Model):
    class Type(models.TextChoices):
        SNACK = "snack", "Colação"
        NURSERY_SNACK = "nursery_snack", "Colação do berçário"
        LUNCH = "lunch", "Almoço"
        DEPARTURE_MEAL = "departure_meal", "Refeição de saída"
        DESSERT = "dessert", "Sobremesa"

    document = models.ForeignKey(MenuDocument, on_delete=models.PROTECT, related_name="meals")
    service_date = models.DateField("Data")
    meal_type = models.CharField("Refeição", max_length=24, choices=Type.choices)
    description = models.TextField("Preparação")

    class Meta:
        ordering = ["service_date", "meal_type", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "service_date", "meal_type"],
                name="unique_menu_meal_per_day",
            )
        ]

    def __str__(self):
        return f"{self.service_date:%d/%m/%Y} — {self.get_meal_type_display()}"


class WeeklyMenuItem(models.Model):
    service_date = models.DateField("Data")
    meal_type = models.CharField("Refeição", max_length=24, choices=MenuMeal.Type.choices)
    source_meal = models.ForeignKey(
        MenuMeal,
        on_delete=models.PROTECT,
        related_name="weekly_menu_items",
        verbose_name="Opção de prato",
    )
    description = models.TextField("Preparação")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="weekly_menu_items_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="weekly_menu_items_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_date", "meal_type", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["service_date", "meal_type"],
                name="unique_weekly_menu_item_per_meal",
            )
        ]

    def __str__(self):
        return f"{self.service_date:%d/%m/%Y} — {self.get_meal_type_display()}"


class DisposalRecord(models.Model):
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="disposal_records",
        verbose_name="Unidade",
    )
    occurred_on = models.DateField("Data do descarte", default=timezone.localdate)
    observation = models.TextField("Observação", max_length=1000)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="disposal_records_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_on", "-created_at", "-pk"]
        indexes = [models.Index(fields=["unit", "occurred_on"])]

    def __str__(self):
        return f"Descarte em {self.unit.code} — {self.occurred_on:%d/%m/%Y}"


class DisposalPhoto(models.Model):
    record = models.ForeignKey(DisposalRecord, on_delete=models.PROTECT, related_name="photos")
    original_name = models.CharField("Nome do arquivo", max_length=180)
    content_type = models.CharField(max_length=30)
    image_content = models.BinaryField(editable=False)
    image_sha256 = models.CharField(max_length=64, editable=False)
    width = models.PositiveIntegerField(editable=False)
    height = models.PositiveIntegerField(editable=False)
    size = models.PositiveIntegerField(editable=False)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "image_sha256"], name="unique_disposal_photo_per_record"
            )
        ]

    def __str__(self):
        return self.original_name


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("A auditoria não pode ser alterada.")

    def delete(self):
        raise ValidationError("A auditoria não pode ser apagada.")


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    actor_name = models.CharField(max_length=160)
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, null=True)
    action = models.CharField(max_length=32)
    entity = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=32)
    description = models.CharField(max_length=240)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["unit", "created_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A auditoria não pode ser alterada.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("A auditoria não pode ser apagada.")
