from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm
from django.db.models import Q
from django.forms import formset_factory
from django.utils import timezone

from .access import visible_units
from .danfe import MAX_PDF_SIZE
from .disposal_images import MAX_DISPOSAL_PHOTO_SIZE
from .menu_pdf import MAX_MENU_PDF_SIZE
from .models import (
    Category,
    DisposalRecord,
    Food,
    MenuMeal,
    Presentation,
    StockMovement,
    Unit,
    UnitAccess,
    UnitFood,
    User,
    normalize_name,
)
from .nfe import MAX_ITEMS, MAX_XML_SIZE
from .stock_services import can_create_movement, movement_kind_choices


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="Usuário",
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "autocomplete": "username",
                "autocapitalize": "none",
            }
        ),
    )
    remember_me = forms.BooleanField(
        label="Lembrar de mim",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "remember-checkbox"}),
    )

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ["name", "code", "fiscal_address_match", "is_active"]
        help_texts = {
            "code": "Identificador único, por exemplo U01. Não use espaços.",
            "fiscal_address_match": (
                "Trecho do endereço que aparece na NF-e. Deixe vazio para usar o nome da unidade."
            ),
        }

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if Unit.objects.filter(code=code).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este código já pertence a uma unidade.")
        return code


class CatalogNameMixin:
    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if (
            self._meta.model.objects.filter(normalized_name=normalize_name(name))
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("Já existe um cadastro com este nome, inclusive inativo.")
        return name


class CategoryForm(CatalogNameMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "is_active"]


class FoodForm(CatalogNameMixin, forms.ModelForm):
    class Meta:
        model = Food
        fields = ["name", "code", "category", "base_unit", "requires_expiry", "is_active"]
        help_texts = {
            "base_unit": "A medida é fixa após o cadastro. Defina embalagens como pacote de 5 kg separadamente.",
            "requires_expiry": "Define a informação necessária ao receber este alimento.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.category_id)
        )
        if self.instance.pk:
            self.fields["base_unit"].disabled = True

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if Food.objects.filter(code=code).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este código já pertence a um alimento.")
        return code


class PresentationForm(forms.ModelForm):
    class Meta:
        model = Presentation
        fields = ["name", "base_quantity", "is_active"]
        localized_fields = ["base_quantity"]
        widgets = {"base_quantity": forms.TextInput(attrs={"inputmode": "decimal"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["base_quantity"].label = f"Conteúdo em {self.instance.food.base_unit}"
        if self.instance.pk:
            self.fields["base_quantity"].disabled = True
            self.fields[
                "base_quantity"
            ].help_text = "Para outro conteúdo, cadastre outra embalagem."

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if (
            Presentation.objects.filter(
                food=self.instance.food, normalized_name=normalize_name(name)
            )
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("Este alimento já tem uma embalagem com esse nome.")
        return name


class UnitFoodForm(forms.ModelForm):
    class Meta:
        model = UnitFood
        fields = ["food", "minimum", "is_active"]
        labels = {"food": "Alimento"}
        localized_fields = ["minimum"]
        widgets = {"minimum": forms.TextInput(attrs={"inputmode": "decimal"})}
        help_texts = {"minimum": "Use a medida indicada no cadastro do alimento (kg, L ou un)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["food"].queryset = Food.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.food_id)
        )
        self.fields["food"].label_from_instance = lambda food: f"{food.name} ({food.base_unit})"
        if self.instance.pk:
            self.fields["food"].disabled = True


class UserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_general_admin", "is_active"]
        labels = {"username": "Usuário", "first_name": "Nome", "last_name": "Sobrenome"}

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        if User.objects.filter(username=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Este usuário já está cadastrado.")
        return username

    def clean(self):
        cleaned = super().clean()
        if self.instance.registration_status != User.RegistrationStatus.APPROVED:
            if cleaned.get("is_active"):
                self.add_error("is_active", "Use a ação Aprovar cadastro antes de ativar a conta.")
            if cleaned.get("is_general_admin"):
                self.add_error(
                    "is_general_admin",
                    "Aprove o cadastro antes de conceder acesso de administrador.",
                )
        return cleaned


class NewUserForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_general_admin"]
        labels = {"username": "Usuário", "first_name": "Nome", "last_name": "Sobrenome"}

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Este usuário já está cadastrado.")
        return username

    def save(self, commit=True):
        user = super().save(commit=False)
        user.registration_status = User.RegistrationStatus.APPROVED
        user.must_change_password = True
        user.temporary_password_expires_at = timezone.now() + timedelta(hours=24)
        if commit:
            user.save()
        return user


class RegistrationForm(UserCreationForm):
    first_name = forms.CharField(label="Nome", max_length=150)
    last_name = forms.CharField(label="Sobrenome", max_length=150)
    email = forms.EmailField(label="E-mail")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ["username", "first_name", "last_name", "email"]
        labels = {"username": "Usuário"}

    def clean_username(self):
        username = self.cleaned_data["username"].strip().lower()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Este usuário já está cadastrado.")
        return username

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = False
        user.is_general_admin = False
        user.is_staff = False
        user.is_superuser = False
        user.registration_status = User.RegistrationStatus.PENDING
        if commit:
            user.save()
        return user


class AccessForm(forms.ModelForm):
    class Meta:
        model = UnitAccess
        fields = ["unit", "role", "is_active"]
        labels = {"unit": "Unidade"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = Unit.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.unit_id)
        )
        if self.instance.pk:
            self.fields["unit"].disabled = True


class ResetUserPasswordForm(SetPasswordForm):
    new_password1 = forms.CharField(label="Nova senha temporária", widget=forms.PasswordInput)
    new_password2 = forms.CharField(label="Repita a senha temporária", widget=forms.PasswordInput)
    identity_confirmed = forms.BooleanField(
        label="Confirmei a identidade da pessoa e entregarei a senha individualmente."
    )


MAX_UPLOAD_FILES = 20
MAX_BATCH_UPLOAD_SIZE = 50 * 1024 * 1024


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            if not data:
                single_clean(None, initial)
            return [single_clean(item, initial) for item in data]
        return [single_clean(data, initial)]


class NFeUploadForm(forms.Form):
    xml_files = MultipleFileField(
        label="Arquivos XML das NF-e",
        help_text="Selecione até 20 XMLs processados e autorizados, com até 2 MB cada.",
        widget=MultipleFileInput(
            attrs={"accept": ".xml,application/xml,text/xml", "multiple": True}
        ),
    )

    def clean_xml_files(self):
        uploads = self.cleaned_data["xml_files"]
        if len(uploads) > MAX_UPLOAD_FILES:
            raise forms.ValidationError(f"Selecione no máximo {MAX_UPLOAD_FILES} arquivos.")
        if sum(uploaded.size for uploaded in uploads) > MAX_BATCH_UPLOAD_SIZE:
            raise forms.ValidationError("O envio conjunto ultrapassa o limite de 50 MB.")
        for uploaded in uploads:
            if not uploaded.name.lower().endswith(".xml"):
                raise forms.ValidationError(f"{uploaded.name}: selecione somente arquivos XML.")
            if uploaded.size > MAX_XML_SIZE:
                raise forms.ValidationError(f"{uploaded.name}: o XML ultrapassa 2 MB.")
        return uploads


class DanfeUploadForm(forms.Form):
    pdf_files = MultipleFileField(
        label="Arquivos PDF dos DANFEs",
        help_text="Selecione até 20 DANFEs, com até 10 MB e 20 páginas cada.",
        widget=MultipleFileInput(attrs={"accept": ".pdf,application/pdf", "multiple": True}),
    )

    def clean_pdf_files(self):
        uploads = self.cleaned_data["pdf_files"]
        if len(uploads) > MAX_UPLOAD_FILES:
            raise forms.ValidationError(f"Selecione no máximo {MAX_UPLOAD_FILES} arquivos.")
        if sum(uploaded.size for uploaded in uploads) > MAX_BATCH_UPLOAD_SIZE:
            raise forms.ValidationError("O envio conjunto ultrapassa o limite de 50 MB.")
        for uploaded in uploads:
            if not uploaded.name.lower().endswith(".pdf"):
                raise forms.ValidationError(f"{uploaded.name}: selecione somente arquivos PDF.")
            if uploaded.size > MAX_PDF_SIZE:
                raise forms.ValidationError(f"{uploaded.name}: o PDF ultrapassa 10 MB.")
        return uploads


class MenuUploadForm(forms.Form):
    menu_files = MultipleFileField(
        label="Arquivos PDF dos cardápios",
        help_text="Selecione até 20 cardápios, com até 10 MB e 10 páginas cada.",
        widget=MultipleFileInput(attrs={"accept": ".pdf,application/pdf", "multiple": True}),
    )

    def clean_menu_files(self):
        uploads = self.cleaned_data["menu_files"]
        if len(uploads) > MAX_UPLOAD_FILES:
            raise forms.ValidationError(f"Selecione no máximo {MAX_UPLOAD_FILES} arquivos.")
        if sum(uploaded.size for uploaded in uploads) > MAX_BATCH_UPLOAD_SIZE:
            raise forms.ValidationError("O envio conjunto ultrapassa o limite de 50 MB.")
        for uploaded in uploads:
            if not uploaded.name.lower().endswith(".pdf"):
                raise forms.ValidationError(f"{uploaded.name}: selecione somente arquivos PDF.")
            if uploaded.size > MAX_MENU_PDF_SIZE:
                raise forms.ValidationError(f"{uploaded.name}: o PDF ultrapassa 10 MB.")
        return uploads


class WeeklyMenuItemForm(forms.Form):
    service_date = forms.DateField(widget=forms.HiddenInput)
    meal_type = forms.ChoiceField(choices=MenuMeal.Type.choices, widget=forms.HiddenInput)
    source_meal = forms.ModelChoiceField(
        label="Prato",
        queryset=MenuMeal.objects.all(),
        empty_label="Selecione uma opção",
    )

    def clean(self):
        cleaned = super().clean()
        service_date = cleaned.get("service_date")
        meal_type = cleaned.get("meal_type")
        source_meal = cleaned.get("source_meal")
        if service_date and service_date.weekday() > 4:
            self.add_error("service_date", "Escolha um dia útil da semana.")
        if source_meal and meal_type and source_meal.meal_type != meal_type:
            self.add_error("source_meal", "Escolha um prato da refeição indicada.")
        return cleaned


class DisposalRecordForm(forms.ModelForm):
    photos = MultipleFileField(
        label="Fotos do descarte",
        help_text="Selecione de 1 a 8 fotos JPEG, PNG ou WebP, com até 8 MB cada.",
        widget=MultipleFileInput(
            attrs={"accept": "image/jpeg,image/png,image/webp", "multiple": True}
        ),
    )

    class Meta:
        model = DisposalRecord
        fields = ["unit", "occurred_on", "observation"]
        widgets = {
            "occurred_on": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "observation": forms.Textarea(
                attrs={"rows": 4, "placeholder": "Descreva o que foi descartado e o motivo."}
            ),
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = (
            visible_units(user).filter(is_active=True).order_by("code", "pk")
        )
        self.fields["unit"].empty_label = "Selecione"
        if not self.is_bound:
            self.initial["occurred_on"] = timezone.localdate()

    def clean_photos(self):
        uploads = self.cleaned_data["photos"]
        if len(uploads) > 8:
            raise forms.ValidationError("Selecione no máximo 8 fotos.")
        if sum(uploaded.size for uploaded in uploads) > 32 * 1024 * 1024:
            raise forms.ValidationError("As fotos juntas ultrapassam o limite de 32 MB.")
        for uploaded in uploads:
            if uploaded.size > MAX_DISPOSAL_PHOTO_SIZE:
                raise forms.ValidationError(f"{uploaded.name}: a foto ultrapassa 8 MB.")
        return uploads

    def clean_observation(self):
        return "\n".join(
            " ".join(line.split())
            for line in self.cleaned_data["observation"].splitlines()
            if line.strip()
        )


class FiscalNewFoodForm(forms.Form):
    item_id = forms.ChoiceField(label="Item da nota")
    name = forms.CharField(label="Nome do alimento", max_length=120)
    category = forms.ModelChoiceField(
        label="Categoria", queryset=Category.objects.none(), empty_label="Selecione"
    )
    base_unit = forms.ChoiceField(label="Unidade de medida", choices=Food.Measure.choices)

    def __init__(self, *args, items=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.items_by_id = {str(item.pk): item for item in items}
        self.fields["item_id"].choices = [
            (str(item.pk), f"{item.line_number}. {item.description}") for item in items
        ]
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")

    def clean_item_id(self):
        item = self.items_by_id.get(self.cleaned_data["item_id"])
        if item is None:
            raise forms.ValidationError("Escolha um item válido desta NF-e.")
        return item

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Food.objects.filter(normalized_name=normalize_name(name)).exists():
            raise forms.ValidationError("Este alimento já está cadastrado. Selecione-o na lista.")
        return name


class FiscalItemMatchForm(forms.Form):
    item_id = forms.IntegerField(widget=forms.HiddenInput)
    food = forms.IntegerField(label="Alimento no estoque", required=False, widget=forms.Select)
    stock_quantity = forms.DecimalField(
        label="Total para distribuir",
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0.001"),
        required=False,
        widget=forms.NumberInput(
            attrs={
                "step": "0.001",
                "inputmode": "decimal",
                "class": "allocation-total",
            }
        ),
    )
    lot_code = forms.CharField(label="Lote (opcional)", max_length=80, required=False)
    expires_on = forms.DateField(
        label="Validade (opcional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    pnae_exception_reason = forms.CharField(
        label="Justificativa para item fora do PNAE",
        max_length=500,
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "class": "pnae-exception-reason",
                "placeholder": "Explique por que este produto precisa ser recebido.",
            }
        ),
    )

    def __init__(self, *args, foods=None, units=None, require_complete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_complete = require_complete
        foods = foods if foods is not None else list(Food.objects.filter(is_active=True))
        self.units = units if units is not None else []
        self.foods_by_id = {food.pk: food for food in foods}
        self.fields["food"].widget.choices = [("", "---------")] + [
            (food.pk, f"{food.code} — {food.name} ({food.base_unit})") for food in foods
        ]
        for unit in self.units:
            self.fields[f"unit_{unit.pk}"] = forms.DecimalField(
                label=f"{unit.code} — {unit.name}",
                max_digits=12,
                decimal_places=3,
                min_value=Decimal("0"),
                required=False,
                widget=forms.NumberInput(
                    attrs={
                        "step": "0.001",
                        "inputmode": "decimal",
                        "class": "allocation-quantity",
                        "data-unit": unit.code,
                    }
                ),
            )

    def clean(self):
        cleaned = super().clean()
        food_id = cleaned.get("food")
        food = self.foods_by_id.get(food_id)
        quantity = cleaned.get("stock_quantity")
        allocations = []
        if food_id is not None and food is None:
            self.add_error("food", "Faça uma escolha válida.")
        if self.require_complete and food is None:
            self.add_error("food", "Selecione o alimento correspondente.")
        if self.require_complete and quantity is None:
            self.add_error("stock_quantity", "Informe o total que será distribuído.")
        cleaned["food"] = food
        for unit in self.units:
            allocated = cleaned.get(f"unit_{unit.pk}")
            if allocated:
                allocations.append((unit, allocated))
        allocated_total = sum((value for _, value in allocations), Decimal("0"))
        cleaned["allocations"] = allocations
        cleaned["allocated_total"] = allocated_total
        if quantity is not None and allocated_total > quantity:
            self.add_error(None, "A distribuição ultrapassa o total informado para o estoque.")
        if self.require_complete and not allocations:
            self.add_error(None, "Distribua este item para pelo menos uma unidade.")
        if self.require_complete and quantity is not None and allocated_total != quantity:
            self.add_error(
                None,
                "A soma distribuída precisa ser igual ao total informado para o estoque.",
            )
        if food and quantity is not None and food.base_unit == Food.Measure.UNIT:
            if quantity != quantity.to_integral_value() or any(
                value != value.to_integral_value() for _, value in allocations
            ):
                self.add_error("stock_quantity", "Este alimento exige uma quantidade inteira.")
        return cleaned


FiscalItemMatchFormSet = formset_factory(
    FiscalItemMatchForm,
    extra=0,
    max_num=MAX_ITEMS,
    validate_max=True,
)


class StockMovementForm(forms.Form):
    unit = forms.ModelChoiceField(label="Unidade", queryset=Unit.objects.none())
    kind = forms.ChoiceField(label="Tipo de movimentação")
    food = forms.ModelChoiceField(label="Alimento", queryset=Food.objects.none())
    quantity = forms.DecimalField(
        label="Quantidade",
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0.001"),
        widget=forms.NumberInput(attrs={"step": "0.001", "inputmode": "decimal"}),
    )
    occurred_at = forms.DateTimeField(
        label="Data e hora",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    lot_code = forms.CharField(label="Lote (opcional)", max_length=80, required=False)
    expires_on = forms.DateField(
        label="Validade (opcional)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Preencha quando essa informação estiver disponível.",
    )
    reason = forms.CharField(
        label="Motivo ou observação",
        max_length=240,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["unit"].queryset = (
            visible_units(user).filter(is_active=True).order_by("code", "pk")
        )
        self.fields["unit"].label_from_instance = lambda unit: f"{unit.code} — {unit.name}"
        self.fields["food"].queryset = Food.objects.filter(is_active=True).order_by("name", "pk")
        self.fields["food"].label_from_instance = lambda food: (
            f"{food.code} — {food.name} ({food.base_unit})"
        )
        self.fields["kind"].choices = movement_kind_choices(user)
        if not self.is_bound and not self.initial.get("occurred_at"):
            self.initial["occurred_at"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def clean(self):
        cleaned = super().clean()
        unit = cleaned.get("unit")
        kind = cleaned.get("kind")
        food = cleaned.get("food")
        quantity = cleaned.get("quantity")
        if unit and kind and not can_create_movement(self.user, unit, kind):
            self.add_error("kind", "Você não pode registrar este tipo nessa unidade.")
        if food and quantity and food.base_unit == Food.Measure.UNIT:
            if quantity != quantity.to_integral_value():
                self.add_error("quantity", "Este alimento exige uma quantidade inteira.")
        if (
            kind
            in {
                StockMovement.Kind.LOSS,
                StockMovement.Kind.SUPPLIER_RETURN,
                StockMovement.Kind.ADJUSTMENT_IN,
                StockMovement.Kind.ADJUSTMENT_OUT,
            }
            and not cleaned.get("reason", "").strip()
        ):
            self.add_error("reason", "Informe o motivo para este tipo de movimentação.")
        return cleaned


class ReversalForm(forms.Form):
    reason = forms.CharField(
        label="Motivo do estorno",
        max_length=240,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class EmployeePreviewForm(forms.Form):
    unit = forms.ModelChoiceField(
        label="Unidade do funcionário",
        queryset=Unit.objects.filter(is_active=True).order_by("code", "pk"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].label_from_instance = lambda unit: f"{unit.code} — {unit.name}"
