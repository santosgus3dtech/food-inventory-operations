import json
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods

from .access import (
    administrator_required,
    can_import_fiscal_documents,
    can_manage_unit,
    get_unit,
    manageable_units,
    require_catalog_access,
    visible_units,
)
from .danfe import DanfeValidationError, parse_danfe_pdf
from .disposal_services import create_disposal_record
from .fiscal_services import (
    approve_fiscal_document_pnae_exceptions,
    confirm_fiscal_document,
    create_fiscal_document,
    create_fiscal_pdf,
    create_food_from_fiscal_item,
    delete_fiscal_document_draft,
    refresh_fiscal_document_suggestions,
    save_fiscal_document_draft,
    visible_fiscal_documents,
    visible_fiscal_pdfs,
)
from .forms import (
    AccessForm,
    CategoryForm,
    DanfeUploadForm,
    DisposalRecordForm,
    EmployeePreviewForm,
    FiscalItemMatchFormSet,
    FiscalNewFoodForm,
    FoodForm,
    LoginForm,
    MenuUploadForm,
    NewUserForm,
    NFeUploadForm,
    PresentationForm,
    RegistrationForm,
    ResetUserPasswordForm,
    ReversalForm,
    StockMovementForm,
    UnitFoodForm,
    UnitForm,
    UserForm,
    WeeklyMenuItemForm,
)
from .menu_pdf import MenuValidationError, parse_menu_pdf
from .menu_services import (
    create_menu_document,
    delete_weekly_menu_item,
    set_weekly_menu_item,
)
from .models import (
    AuditEvent,
    Category,
    DisposalPhoto,
    DisposalRecord,
    FiscalDocument,
    FiscalPnaeException,
    Food,
    MenuDocument,
    MenuMeal,
    PnaeCatalog,
    PnaeCatalogItem,
    Presentation,
    StockMovement,
    Unit,
    UnitAccess,
    UnitFood,
    User,
    WeeklyMenuItem,
)
from .nfe import NFeValidationError, parse_nfe_xml
from .pnae import active_pnae_catalog, active_pnae_food_ids, pnae_item_by_food
from .price_history import purchase_price_history
from .product_updates import PRODUCT_UPDATES
from .services import audit, lock_user_management, save_user, snapshot
from .stock_services import create_stock_movement, reverse_stock_movement


class SignInView(LoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        expiry = settings.REMEMBER_ME_SESSION_AGE if form.cleaned_data["remember_me"] else 0
        self.request.session.set_expiry(expiry)
        return response


class SignOutView(LogoutView):
    pass


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                person = form.save()
                audit(
                    None,
                    person,
                    "registration_requested",
                    description=f"Cadastro solicitado: {person.display_name}",
                )
        except IntegrityError:
            form.add_error("username", "Este usuário já está cadastrado.")
        else:
            return redirect("registration_submitted")
    return render(request, "registration/register.html", {"title": "Criar conta", "form": form})


@require_GET
def registration_submitted(request):
    return render(request, "registration/submitted.html", {"title": "Cadastro enviado"})


@require_GET
def health(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok", "application": "food-inventory-operations"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


def paginate(request, objects):
    return Paginator(objects, 25).get_page(request.GET.get("page"))


def filtered(request, objects, fields=("name",)):
    query = request.GET.get("q", "").strip()[:120]
    if query:
        condition = Q()
        for field in fields:
            condition |= Q(**{f"{field}__icontains": query})
        objects = objects.filter(condition)
    status = request.GET.get("status", "")
    if status in ("active", "inactive"):
        objects = objects.filter(is_active=status == "active")
    return objects


@login_required
@require_GET
def dashboard(request):
    units_qs = visible_units(request.user).filter(is_active=True)
    stock = UnitFood.objects.filter(unit__in=units_qs, is_active=True, food__is_active=True)
    recent_movements = (
        StockMovement.objects.filter(unit__in=units_qs)
        .select_related("unit", "created_by")
        .prefetch_related("items__unit_food__food")[:8]
    )
    pending_documents = 0
    if can_import_fiscal_documents(request.user):
        pending_documents = (
            visible_fiscal_documents(request.user)
            .filter(status=FiscalDocument.Status.REVIEW)
            .count()
            + visible_fiscal_pdfs(request.user).filter(document__isnull=True).count()
        )
    return render(
        request,
        "core/dashboard.html",
        {
            "title": "Painel",
            "units_count": units_qs.count(),
            "zero_stock_count": stock.filter(quantity=0).count(),
            "low_stock_count": stock.filter(quantity__gt=0, quantity__lt=F("minimum")).count(),
            "pending_documents_count": pending_documents,
            "pending_users_count": User.objects.filter(
                registration_status=User.RegistrationStatus.PENDING
            ).count()
            if request.user.is_administrator
            else None,
            "recent_movements": recent_movements,
            "first_unit": units_qs.order_by("code", "pk").first(),
        },
    )


@login_required
@require_GET
def units(request):
    rows = filtered(request, visible_units(request.user), ("name", "code")).order_by("code", "pk")
    return render(
        request,
        "core/units.html",
        {
            "page": paginate(request, rows),
            "title": "Unidades" if request.user.is_administrator else "Estoque",
        },
    )


@login_required
@require_GET
def stock_alerts(request):
    rows = UnitFood.objects.filter(
        unit__in=visible_units(request.user),
        unit__is_active=True,
        is_active=True,
        food__is_active=True,
    ).select_related("unit", "food")
    status = request.GET.get("status", "zero")
    if status == "low":
        rows = rows.filter(quantity__gt=0, quantity__lt=F("minimum"))
    else:
        status = "zero"
        rows = rows.filter(quantity=0)
    rows = rows.order_by("unit__code", "food__name", "pk")
    return render(
        request,
        "core/stock_alerts.html",
        {"title": "Atenção do estoque", "page": paginate(request, rows), "status": status},
    )


def edit_record(request, model, form_class, title, back_url, pk=None, instance=None, unit=None):
    """Bind and save inside the same transaction/row lock, including audit."""
    with transaction.atomic():
        obj = (
            get_object_or_404(model.objects.select_for_update(), pk=pk)
            if pk
            else instance or model()
        )
        before = snapshot(obj) if obj.pk else {}
        form = form_class(request.POST if request.method == "POST" else None, instance=obj)
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    saved = form.save()
                    audit(request.user, saved, "updated" if pk else "created", before, unit)
                messages.success(request, "Cadastro salvo com sucesso.")
                return redirect(back_url)
            except IntegrityError:
                form.add_error(
                    None,
                    "Este cadastro já existe. Confira o nome, o código ou o vínculo informado.",
                )
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": title,
            "back_url": back_url,
        },
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def unit_edit(request, pk=None):
    return edit_record(
        request, Unit, UnitForm, "Editar unidade" if pk else "Nova unidade", reverse("units"), pk=pk
    )


@login_required
@require_GET
def unit_detail(request, pk):
    unit = get_unit(request.user, pk)
    can_manage = can_manage_unit(request.user, unit)
    items = UnitFood.objects.filter(unit=unit).select_related("food", "food__category")
    if not can_manage:
        items = items.filter(is_active=True, food__is_active=True)
    items = filtered(request, items, ("food__name", "food__code"))
    items = items.annotate(
        stock_order=Case(
            When(quantity__gt=0, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by("stock_order", "food__name", "pk")
    return render(
        request,
        "core/unit_detail.html",
        {
            "unit": unit,
            "title": unit.name,
            "page": paginate(request, items),
            "can_manage": can_manage,
        },
    )


@login_required
@require_http_methods(["POST"])
def adjust_unit_food_quantity(request, unit_pk, pk):
    unit = get_unit(request.user, unit_pk)
    action = request.POST.get("action")
    if action not in {"increase", "decrease"}:
        return HttpResponseBadRequest("Ajuste inválido.")

    with transaction.atomic():
        item = get_object_or_404(
            UnitFood.objects.select_for_update().select_related("food"),
            pk=pk,
            unit=unit,
            is_active=True,
            food__is_active=True,
        )
        before = snapshot(item)
        if action == "decrease" and item.quantity <= 0:
            movement = None
        else:
            movement = create_stock_movement(
                unit=unit,
                kind=(
                    StockMovement.Kind.RECEIPT
                    if action == "increase"
                    else StockMovement.Kind.CONSUMPTION
                ),
                lines=[{"food": item.food, "quantity": Decimal("1")}],
                actor=request.user,
                reason="Ajuste rápido pela tela da unidade",
            )
        if movement:
            item.refresh_from_db()
            audit(
                request.user,
                item,
                "quantity_adjusted",
                before,
                unit,
                description=f"{item.food.name}: {item.quantity} {item.food.base_unit}",
            )

    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("unit_detail", args=[unit.pk])
    return redirect(f"{next_url}#food-{item.pk}")


def visible_movements(user):
    return StockMovement.objects.filter(unit__in=visible_units(user))


@login_required
@require_GET
def stock_movements(request):
    rows = (
        visible_movements(request.user)
        .select_related("unit", "created_by")
        .prefetch_related("items__unit_food__food")
    )
    query = request.GET.get("q", "").strip()[:120]
    if query:
        rows = rows.filter(
            Q(reason__icontains=query)
            | Q(unit__name__icontains=query)
            | Q(items__unit_food__food__name__icontains=query)
        ).distinct()
    kind = request.GET.get("kind", "")
    if kind in StockMovement.Kind.values:
        rows = rows.filter(kind=kind)
    return render(
        request,
        "core/stock_movements.html",
        {
            "title": "Movimentações",
            "page": paginate(request, rows),
            "movement_kinds": StockMovement.Kind.choices,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def stock_movement_create(request):
    initial = {}
    if request.method == "GET":
        initial = {
            "unit": request.GET.get("unit", ""),
            "food": request.GET.get("food", ""),
            "kind": request.GET.get("kind", StockMovement.Kind.CONSUMPTION),
        }
    form = StockMovementForm(
        request.POST if request.method == "POST" else None,
        user=request.user,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            movement = create_stock_movement(
                unit=form.cleaned_data["unit"],
                kind=form.cleaned_data["kind"],
                lines=[
                    {
                        "food": form.cleaned_data["food"],
                        "quantity": form.cleaned_data["quantity"],
                        "lot_code": form.cleaned_data["lot_code"],
                        "expires_on": form.cleaned_data["expires_on"],
                    }
                ],
                actor=request.user,
                occurred_at=form.cleaned_data["occurred_at"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Movimentação registrada e saldo atualizado.")
            return redirect("stock_movement_detail", pk=movement.pk)
    return render(
        request,
        "core/form.html",
        {
            "title": "Nova movimentação",
            "intro": "Registre a operação real. Lote e validade são opcionais.",
            "form": form,
            "back_url": reverse("stock_movements"),
        },
    )


@login_required
@require_GET
def stock_movement_detail(request, pk):
    movement = get_object_or_404(
        visible_movements(request.user)
        .select_related("unit", "created_by", "fiscal_document", "reversed_movement")
        .prefetch_related("items__unit_food__food"),
        pk=pk,
    )
    can_reverse = (
        can_manage_unit(request.user, movement.unit)
        and movement.kind not in {StockMovement.Kind.OPENING, StockMovement.Kind.REVERSAL}
        and not StockMovement.objects.filter(reversed_movement=movement).exists()
    )
    return render(
        request,
        "core/stock_movement_detail.html",
        {"title": f"Movimentação #{movement.pk}", "movement": movement, "can_reverse": can_reverse},
    )


@login_required
@require_http_methods(["GET", "POST"])
def stock_movement_reverse(request, pk):
    movement = get_object_or_404(visible_movements(request.user).select_related("unit"), pk=pk)
    if not can_manage_unit(request.user, movement.unit):
        raise PermissionDenied
    form = ReversalForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            reversal = reverse_stock_movement(
                movement=movement, actor=request.user, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Estorno registrado como uma nova movimentação.")
            return redirect("stock_movement_detail", pk=reversal.pk)
    return render(
        request,
        "core/form.html",
        {
            "title": f"Estornar movimentação #{movement.pk}",
            "intro": "O registro original será preservado e o efeito será invertido.",
            "form": form,
            "back_url": reverse("stock_movement_detail", args=[movement.pk]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def unit_food_edit(request, unit_pk, pk=None):
    unit = get_unit(request.user, unit_pk)
    if not can_manage_unit(request.user, unit):
        raise PermissionDenied
    if pk:
        get_object_or_404(UnitFood, pk=pk, unit=unit)
    return edit_record(
        request,
        UnitFood,
        UnitFoodForm,
        "Editar alimento da unidade" if pk else "Acompanhar alimento",
        reverse("unit_detail", args=[unit.pk]),
        pk=pk,
        instance=UnitFood(unit=unit, minimum=0),
        unit=unit,
    )


@login_required
@require_GET
def foods(request):
    require_catalog_access(request.user)
    rows = filtered(request, Food.objects.select_related("category"), ("name", "code"))
    return render(
        request, "core/foods.html", {"title": "Alimentos", "page": paginate(request, rows)}
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def food_edit(request, pk=None):
    return edit_record(
        request,
        Food,
        FoodForm,
        "Editar alimento" if pk else "Novo alimento",
        reverse("food_detail", args=[pk]) if pk else reverse("foods"),
        pk=pk,
    )


@login_required
@require_GET
def food_detail(request, pk):
    require_catalog_access(request.user)
    food = get_object_or_404(Food.objects.select_related("category"), pk=pk)
    price_history = None
    if can_import_fiscal_documents(request.user):
        price_history = purchase_price_history(food, request.user)
    return render(
        request,
        "core/food_detail.html",
        {
            "food": food,
            "title": food.name,
            "presentations": food.presentations.all(),
            "price_history": price_history,
        },
    )


@administrator_required
@require_GET
def pnae_catalog(request):
    catalog = active_pnae_catalog() or PnaeCatalog.objects.order_by("-year").first()
    rows = PnaeCatalogItem.objects.none()
    pending_exceptions = FiscalPnaeException.objects.none()
    if catalog:
        rows = PnaeCatalogItem.objects.filter(catalog=catalog).select_related(
            "food", "food__category"
        )
        query = request.GET.get("q", "").strip()[:120]
        if query:
            rows = rows.filter(
                Q(code__icontains=query)
                | Q(name__icontains=query)
                | Q(specification__icontains=query)
                | Q(food__name__icontains=query)
                | Q(food__code__icontains=query)
            )
        pending_exceptions = (
            FiscalPnaeException.objects.filter(
                catalog=catalog,
                status=FiscalPnaeException.Status.PENDING,
            )
            .select_related("item__document", "food", "requested_by")
            .order_by("requested_at", "pk")
        )
    return render(
        request,
        "core/pnae_catalog.html",
        {
            "title": "Catálogo PNAE",
            "catalog": catalog,
            "page": paginate(request, rows),
            "total_items": catalog.items.count() if catalog else 0,
            "linked_foods": catalog.items.values("food_id").distinct().count() if catalog else 0,
            "pending_exceptions": pending_exceptions,
        },
    )


@administrator_required
@require_GET
def product_updates(request):
    return render(
        request,
        "core/product_updates.html",
        {
            "title": "Atualizações",
            "updates": PRODUCT_UPDATES,
        },
    )


WEEKDAYS_PT = (
    "Segunda-feira",
    "Terça-feira",
    "Quarta-feira",
    "Quinta-feira",
    "Sexta-feira",
    "Sábado",
    "Domingo",
)


def _menu_days(document):
    if not document:
        return []
    meals = list(document.meals.all())
    by_date = {}
    for meal in meals:
        by_date.setdefault(meal.service_date, {})[meal.meal_type] = meal
    dates = sorted(by_date)
    return [
        {
            "date": service_date,
            "weekday": WEEKDAYS_PT[service_date.weekday()],
            "meals": [
                by_date[service_date].get(meal_type) for meal_type, _label in MenuMeal.Type.choices
            ],
        }
        for service_date in dates
    ]


def _week_start(value=None):
    selected = value or timezone.localdate()
    return selected - timedelta(days=selected.weekday())


def _requested_week_start(request):
    try:
        selected = date.fromisoformat(request.GET.get("semana", ""))
    except ValueError:
        selected = None
    return _week_start(selected)


def _menu_dish_options():
    grouped = {meal_type: [] for meal_type, _label in MenuMeal.Type.choices}
    labels = dict(MenuMeal.Type.choices)
    seen = set()
    for meal in MenuMeal.objects.exclude(description__in=["", "-"]).order_by(
        "meal_type", "description", "pk"
    ):
        display = " / ".join(" ".join(line.split()) for line in meal.description.splitlines())
        key = (meal.meal_type, " ".join(meal.description.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        grouped[meal.meal_type].append(
            {
                "source_meal_id": meal.pk,
                "description": meal.description,
                "display": display,
            }
        )
    for options in grouped.values():
        options.sort(key=lambda option: option["display"].casefold())
    return [
        {
            "meal_type": meal_type,
            "label": labels[meal_type],
            "options": grouped[meal_type],
        }
        for meal_type, _label in MenuMeal.Type.choices
    ]


def _weekly_menu_plan(week_start, dish_groups):
    days = [week_start + timedelta(days=offset) for offset in range(5)]
    items = {
        (item.service_date, item.meal_type): item
        for item in WeeklyMenuItem.objects.filter(
            service_date__gte=days[0], service_date__lte=days[-1]
        ).select_related("source_meal", "updated_by")
    }
    options_by_type = {group["meal_type"]: group["options"] for group in dish_groups}
    labels = dict(MenuMeal.Type.choices)
    return {
        "week_start": days[0],
        "week_end": days[-1],
        "previous_week": days[0] - timedelta(days=7),
        "next_week": days[0] + timedelta(days=7),
        "days": [{"date": day, "weekday": WEEKDAYS_PT[day.weekday()]} for day in days],
        "rows": [
            {
                "meal_type": meal_type,
                "label": labels[meal_type],
                "cells": [
                    {
                        "date": day,
                        "item": items.get((day, meal_type)),
                        "options": options_by_type[meal_type],
                    }
                    for day in days
                ],
            }
            for meal_type, _label in MenuMeal.Type.choices
        ],
    }


@login_required
@require_GET
def menu_documents(request):
    documents = (
        MenuDocument.objects.select_related("created_by")
        .prefetch_related("meals")
        .order_by(F("week_start").desc(nulls_last=True), "-created_at", "-pk")
    )
    today = timezone.localdate()
    current = (
        documents.filter(week_start__lte=today, week_end__gte=today)
        .order_by("-week_start", "-created_at")
        .first()
        or documents.first()
    )
    archive = documents.exclude(pk=current.pk) if current else documents
    dish_groups = _menu_dish_options()
    planning_week = _requested_week_start(request)
    return render(
        request,
        "core/menu_documents.html",
        {
            "title": "Cardápio",
            "current_menu": current,
            "menu_days": _menu_days(current),
            "menu_plan": _weekly_menu_plan(planning_week, dish_groups),
            "dish_groups": dish_groups,
            "page": paginate(request, archive),
        },
    )


def _menu_plan_redirect(value):
    return redirect(
        f"{reverse('menu_documents')}?semana={_week_start(value):%Y-%m-%d}#planejamento"
    )


@administrator_required
@require_http_methods(["POST"])
def weekly_menu_item_save(request):
    form = WeeklyMenuItemForm(request.POST)
    if form.is_valid():
        try:
            _item, created = set_weekly_menu_item(
                service_date=form.cleaned_data["service_date"],
                meal_type=form.cleaned_data["meal_type"],
                source_meal=form.cleaned_data["source_meal"],
                actor=request.user,
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            messages.success(
                request,
                "Prato adicionado ao cardápio." if created else "Prato atualizado no cardápio.",
            )
        return _menu_plan_redirect(form.cleaned_data["service_date"])

    messages.error(request, "Não foi possível adicionar o prato. Confira a opção escolhida.")
    try:
        selected = date.fromisoformat(request.POST.get("week_start", ""))
    except ValueError:
        selected = None
    return _menu_plan_redirect(selected or timezone.localdate())


@administrator_required
@require_http_methods(["POST"])
def weekly_menu_item_delete(request, pk):
    item = get_object_or_404(WeeklyMenuItem, pk=pk)
    service_date = item.service_date
    delete_weekly_menu_item(item=item, actor=request.user)
    messages.success(request, "Prato removido do cardápio.")
    return _menu_plan_redirect(service_date)


@login_required
@require_GET
def menu_document_detail(request, pk):
    document = get_object_or_404(
        MenuDocument.objects.select_related("created_by").prefetch_related("meals"), pk=pk
    )
    return render(
        request,
        "core/menu_document_detail.html",
        {
            "title": "Cardápio",
            "menu": document,
            "menu_days": _menu_days(document),
        },
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def menu_document_upload(request):
    form = MenuUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        documents = []
        created_count = duplicate_count = error_count = 0
        uploads = form.cleaned_data["menu_files"]
        for uploaded in uploads:
            try:
                parsed = parse_menu_pdf(uploaded.read(), uploaded.name)
                document, created = create_menu_document(parsed, uploaded.name, request.user)
            except MenuValidationError as exc:
                error_count += 1
                messages.error(request, f"{uploaded.name}: {exc}")
                continue
            documents.append(document)
            if created:
                created_count += 1
            else:
                duplicate_count += 1
        if created_count:
            messages.success(
                request,
                f"{created_count} cardápio(s) recebido(s) e salvo(s) como referência.",
            )
        if duplicate_count:
            messages.info(
                request, f"{duplicate_count} arquivo(s) já existiam e não foram duplicados."
            )
        if len(uploads) == 1 and len(documents) == 1 and not error_count:
            return redirect("menu_document_detail", pk=documents[0].pk)
        if documents:
            return redirect("menu_documents")
    return render(
        request,
        "core/menu_document_upload.html",
        {"title": "Enviar cardápio", "form": form},
    )


@login_required
@require_GET
def menu_document_download(request, pk):
    document = get_object_or_404(MenuDocument, pk=pk)
    response = HttpResponse(bytes(document.pdf_content), content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(True, document.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _visible_disposal_records(user):
    return DisposalRecord.objects.filter(unit__in=visible_units(user)).select_related(
        "unit", "created_by"
    )


@login_required
@require_GET
def disposal_records(request):
    require_catalog_access(request.user)
    rows = _visible_disposal_records(request.user).prefetch_related("photos")
    unit_id = request.GET.get("unit", "")
    if unit_id.isdigit():
        rows = rows.filter(unit_id=unit_id)
    return render(
        request,
        "core/disposal_records.html",
        {
            "title": "Descarte",
            "page": paginate(request, rows),
            "units": visible_units(request.user).filter(is_active=True).order_by("code", "pk"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def disposal_record_create(request):
    require_catalog_access(request.user)
    form = DisposalRecordForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        user=request.user,
    )
    if request.method == "POST" and form.is_valid():
        try:
            record = create_disposal_record(
                unit=form.cleaned_data["unit"],
                occurred_on=form.cleaned_data["occurred_on"],
                observation=form.cleaned_data["observation"],
                uploads=form.cleaned_data["photos"],
                actor=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Descarte registrado com as fotos e a observação.")
            return redirect("disposal_record_detail", pk=record.pk)
    return render(
        request,
        "core/disposal_record_form.html",
        {"title": "Registrar descarte", "form": form},
    )


@login_required
@require_GET
def disposal_record_detail(request, pk):
    require_catalog_access(request.user)
    record = get_object_or_404(
        _visible_disposal_records(request.user).prefetch_related("photos"), pk=pk
    )
    return render(
        request,
        "core/disposal_record_detail.html",
        {"title": "Descarte", "record": record},
    )


@login_required
@require_GET
def disposal_photo(request, pk, photo_pk):
    require_catalog_access(request.user)
    photo = get_object_or_404(
        DisposalPhoto.objects.select_related("record__unit"),
        pk=photo_pk,
        record_id=pk,
        record__unit__in=visible_units(request.user),
    )
    response = HttpResponse(bytes(photo.image_content), content_type=photo.content_type)
    response["Content-Disposition"] = content_disposition_header(False, photo.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    return response


def require_fiscal_document_access(user):
    if not can_import_fiscal_documents(user):
        raise PermissionDenied


@login_required
@require_GET
def fiscal_documents(request):
    require_fiscal_document_access(request.user)
    rows = (
        visible_fiscal_documents(request.user)
        .select_related("unit", "created_by")
        .prefetch_related("stock_movements__unit")
    )
    query = request.GET.get("q", "").strip()[:120]
    if query:
        rows = rows.filter(
            Q(number__icontains=query)
            | Q(access_key__icontains=query)
            | Q(supplier_name__icontains=query)
            | Q(supplier_cnpj__icontains=query)
        )
    status = request.GET.get("status", "")
    if status in FiscalDocument.Status.values:
        rows = rows.filter(status=status)
    return render(
        request,
        "core/fiscal_documents.html",
        {
            "title": "Notas fiscais",
            "page": paginate(request, rows),
            "pending_pdfs": visible_fiscal_pdfs(request.user)
            .filter(document__isnull=True)
            .select_related("created_by")[:20],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def fiscal_document_upload(request):
    require_fiscal_document_access(request.user)
    form = NFeUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        documents = []
        created_count = 0
        duplicate_count = 0
        error_count = 0
        uploads = form.cleaned_data["xml_files"]
        for uploaded in uploads:
            try:
                parsed = parse_nfe_xml(uploaded.read())
                document, created = create_fiscal_document(parsed, request.user)
            except NFeValidationError as exc:
                error_count += 1
                messages.error(request, f"{uploaded.name}: {exc}")
                continue
            documents.append(document)
            if created:
                created_count += 1
            else:
                duplicate_count += 1
        if created_count:
            messages.success(
                request,
                f"{created_count} XML(s) recebido(s) para conferência. Nenhum saldo foi alterado.",
            )
        if duplicate_count:
            messages.info(
                request,
                f"{duplicate_count} NF-e(s) já existiam e não foram duplicadas.",
            )
        if len(uploads) == 1 and len(documents) == 1 and not error_count:
            return redirect("fiscal_document_detail", pk=documents[0].pk)
        if documents:
            return redirect("fiscal_documents")
    return render(
        request,
        "core/fiscal_document_upload.html",
        {"title": "Receber NF-e", "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def fiscal_pdf_upload(request):
    require_fiscal_document_access(request.user)
    form = DanfeUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        linked_documents = []
        created_count = 0
        duplicate_count = 0
        error_count = 0
        uploads = form.cleaned_data["pdf_files"]
        for uploaded in uploads:
            try:
                parsed = parse_danfe_pdf(uploaded.read())
                fiscal_pdf, created = create_fiscal_pdf(parsed, uploaded.name, request.user)
            except (DanfeValidationError, ValidationError) as exc:
                error_count += 1
                messages.error(request, f"{uploaded.name}: {exc}")
                continue
            if fiscal_pdf.document_id:
                linked_documents.append(fiscal_pdf.document)
            if created:
                created_count += 1
            else:
                duplicate_count += 1
        if created_count:
            messages.success(
                request,
                f"{created_count} PDF(s) recebido(s). PDFs sem XML ficaram aguardando o arquivo autorizado.",
            )
        if duplicate_count:
            messages.info(request, f"{duplicate_count} PDF(s) já existiam e não foram duplicados.")
        if len(uploads) == 1 and len(linked_documents) == 1 and not error_count:
            document = linked_documents[0]
            if visible_fiscal_documents(request.user).filter(pk=document.pk).exists():
                return redirect("fiscal_document_detail", pk=document.pk)
        if created_count or duplicate_count:
            return redirect("fiscal_documents")
    return render(
        request,
        "core/fiscal_pdf_upload.html",
        {"title": "Receber PDF", "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def fiscal_document_detail(request, pk):
    require_fiscal_document_access(request.user)
    document = get_object_or_404(
        visible_fiscal_documents(request.user)
        .select_related("unit", "created_by", "confirmed_by", "draft_saved_by")
        .prefetch_related(
            "stock_movements__unit",
            "items__stock_movement_items__movement__unit",
        ),
        pk=pk,
    )
    refresh_fiscal_document_suggestions(document.pk, request.user)
    items = list(
        document.items.select_related(
            "food",
            "pnae_exception__catalog",
            "pnae_exception__food",
            "pnae_exception__requested_by",
            "pnae_exception__reviewed_by",
        ).order_by("line_number")
    )
    active_foods = list(Food.objects.filter(is_active=True).order_by("name", "pk"))
    units = list(manageable_units(request.user).order_by("code", "pk"))
    pnae_catalog = active_pnae_catalog()
    pnae_food_ids = active_pnae_food_ids(pnae_catalog)
    pnae_items_by_food = pnae_item_by_food(pnae_catalog)

    if request.method == "POST" and document.status == FiscalDocument.Status.IMPORTED:
        messages.info(request, "Esta NF-e já foi confirmada e não alterou o estoque novamente.")
        return redirect("fiscal_document_detail", pk=document.pk)

    action = request.POST.get("action", "confirm") if request.method == "POST" else ""
    if action == "delete_draft":
        document, deleted = delete_fiscal_document_draft(document.pk, request.user)
        if deleted:
            messages.success(
                request,
                "Rascunho excluído. O XML e a NF-e continuam disponíveis e o estoque não mudou.",
            )
        else:
            messages.info(request, "Esta NF-e não possui um rascunho salvo.")
        return redirect("fiscal_document_detail", pk=document.pk)

    draft_items = document.review_draft.get("items", {}) if document.review_draft else {}
    initial_items = []
    for item in items:
        initial = {
            "item_id": item.pk,
            "food": item.food_id,
            "stock_quantity": item.stock_quantity,
            "lot_code": item.lot_code,
            "expires_on": item.expires_on,
            "pnae_exception_reason": "",
        }
        try:
            pnae_exception = item.pnae_exception
        except FiscalPnaeException.DoesNotExist:
            pnae_exception = None
        if pnae_exception and pnae_exception.food_id == item.food_id:
            initial["pnae_exception_reason"] = pnae_exception.reason
        draft = draft_items.get(str(item.pk), {})
        if draft:
            initial.update(
                {
                    "food": draft.get("food_id"),
                    "stock_quantity": draft.get("stock_quantity"),
                    "lot_code": draft.get("lot_code", ""),
                    "expires_on": draft.get("expires_on", ""),
                    "pnae_exception_reason": draft.get("pnae_exception_reason", ""),
                }
            )
            for unit_id, quantity in draft.get("allocations", {}).items():
                initial[f"unit_{unit_id}"] = quantity
        elif document.unit_id and item.stock_quantity:
            initial[f"unit_{document.unit_id}"] = item.stock_quantity
        initial_items.append(initial)

    new_food_form = FiscalNewFoodForm(
        request.POST if action == "create_food" else None,
        prefix="new_food",
        items=items,
        initial={"item_id": next((item.pk for item in items if not item.food_id), items[0].pk)},
    )
    if action == "create_food":
        if not request.user.is_administrator:
            raise PermissionDenied
        if new_food_form.is_valid():
            food = create_food_from_fiscal_item(
                document=document,
                item=new_food_form.cleaned_data["item_id"],
                name=new_food_form.cleaned_data["name"],
                category=new_food_form.cleaned_data["category"],
                base_unit=new_food_form.cleaned_data["base_unit"],
                actor=request.user,
            )
            messages.success(
                request,
                f"{food.code} — {food.name} foi criado e relacionado ao item da nota.",
            )
            return redirect("fiscal_document_detail", pk=document.pk)

    review_actions = {"save_draft", "approve_pnae_exceptions", "confirm"}
    item_formset = FiscalItemMatchFormSet(
        request.POST if action in review_actions else None,
        prefix="items",
        initial=initial_items,
        form_kwargs={
            "foods": active_foods,
            "units": units,
            "require_complete": action == "confirm",
        },
    )
    if action in review_actions and len(item_formset.forms) != len(items):
        return HttpResponseBadRequest("A lista de itens da NF-e é inválida.")
    if action in review_actions and item_formset.is_valid():
        matches = [
            {
                "item_id": row.cleaned_data["item_id"],
                "food": row.cleaned_data["food"],
                "stock_quantity": row.cleaned_data["stock_quantity"],
                "lot_code": row.cleaned_data["lot_code"],
                "expires_on": row.cleaned_data["expires_on"],
                "allocations": row.cleaned_data["allocations"],
                "pnae_exception_reason": row.cleaned_data["pnae_exception_reason"],
            }
            for row in item_formset
        ]
        try:
            if action == "save_draft":
                document, saved = save_fiscal_document_draft(document.pk, matches, request.user)
                imported = False
                approved = None
            elif action == "approve_pnae_exceptions":
                document, approved = approve_fiscal_document_pnae_exceptions(
                    document.pk, matches, request.user
                )
                saved = False
                imported = False
            else:
                document, imported = confirm_fiscal_document(document.pk, matches, request.user)
                saved = False
                approved = None
        except ValidationError as exc:
            item_formset.forms[0].add_error(None, exc)
        else:
            if approved is not None:
                messages.success(
                    request,
                    f"Rascunho salvo e {approved} exceção(ões) PNAE aprovada(s).",
                )
            elif saved:
                messages.success(
                    request,
                    "Rascunho salvo. Nenhuma quantidade foi adicionada ao estoque.",
                )
            elif imported:
                messages.success(
                    request,
                    f"NF-e {document.number} confirmada. As entradas foram registradas por unidade.",
                )
            else:
                messages.info(request, "Esta NF-e já havia sido confirmada.")
            return redirect("fiscal_document_detail", pk=document.pk)

    item_rows = []
    for item, form in zip(items, item_formset.forms, strict=True):
        selected_food_id = form.initial.get("food")
        try:
            pnae_exception = item.pnae_exception
        except FiscalPnaeException.DoesNotExist:
            pnae_exception = None
        compliance = {
            "catalog_item": pnae_items_by_food.get(selected_food_id),
            "exception": pnae_exception,
            "is_outside": bool(selected_food_id and selected_food_id not in pnae_food_ids),
        }
        item_rows.append(
            (
                item,
                form,
                [(unit, form[f"unit_{unit.pk}"]) for unit in units],
                compliance,
            )
        )
    confirmed_rows = [(item, list(item.stock_movement_items.all())) for item in items]
    return render(
        request,
        "core/fiscal_document_detail.html",
        {
            "title": f"NF-e {document.number}",
            "document": document,
            "item_rows": item_rows,
            "confirmed_rows": confirmed_rows,
            "item_formset": item_formset,
            "units": units,
            "new_food_form": new_food_form,
            "can_create_food": request.user.is_administrator,
            "pnae_catalog": pnae_catalog,
            "pnae_food_ids": sorted(pnae_food_ids),
        },
    )


@login_required
@require_GET
def fiscal_document_download(request, pk):
    require_fiscal_document_access(request.user)
    document = get_object_or_404(visible_fiscal_documents(request.user), pk=pk)
    response = HttpResponse(bytes(document.xml_content), content_type="application/xml")
    response["Content-Disposition"] = f'attachment; filename="NFe{document.access_key}.xml"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_GET
def fiscal_pdf_download(request, pk):
    require_fiscal_document_access(request.user)
    fiscal_pdf = get_object_or_404(visible_fiscal_pdfs(request.user), pk=pk)
    response = HttpResponse(bytes(fiscal_pdf.pdf_content), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="DANFE-{fiscal_pdf.access_key}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@administrator_required
@require_http_methods(["GET", "POST"])
def presentation_edit(request, food_pk, pk=None):
    food = get_object_or_404(Food, pk=food_pk)
    if pk:
        get_object_or_404(Presentation, pk=pk, food=food)
    return edit_record(
        request,
        Presentation,
        PresentationForm,
        "Editar embalagem" if pk else "Nova embalagem",
        reverse("food_detail", args=[food.pk]),
        pk=pk,
        instance=Presentation(food=food),
    )


@administrator_required
@require_GET
def categories(request):
    rows = filtered(request, Category.objects.all())
    return render(
        request, "core/categories.html", {"title": "Categorias", "page": paginate(request, rows)}
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def category_edit(request, pk=None):
    return edit_record(
        request,
        Category,
        CategoryForm,
        "Editar categoria" if pk else "Nova categoria",
        reverse("categories"),
        pk=pk,
    )


@administrator_required
@require_GET
def users(request):
    rows = filtered(
        request,
        User.objects.filter(is_superuser=False).order_by("first_name", "username"),
        ("username", "first_name", "last_name"),
    )
    registration = request.GET.get("registration", "")
    if registration in User.RegistrationStatus.values:
        rows = rows.filter(registration_status=registration)
    return render(
        request, "core/users.html", {"title": "Usuários", "page": paginate(request, rows)}
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def user_edit(request, pk=None):
    with transaction.atomic():
        lock_user_management()
        obj = (
            get_object_or_404(User.objects.select_for_update(), pk=pk, is_superuser=False)
            if pk
            else None
        )
        form = (UserForm if pk else NewUserForm)(
            request.POST if request.method == "POST" else None,
            instance=obj,
        )
        if request.method == "POST" and form.is_valid():
            try:
                saved = save_user(form, request.user)
                messages.success(request, "Usuário salvo. Configure os acessos às unidades.")
                return redirect("user_detail", pk=saved.pk)
            except ValidationError as exc:
                form.add_error(None, exc)
            except IntegrityError:
                form.add_error(None, "Este usuário já está cadastrado.")
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": "Editar usuário" if pk else "Novo usuário",
            "back_url": reverse("users"),
            "intro": "A senha inicial é individual, expira em 24 horas e deve ser alterada no primeiro acesso."
            if not pk
            else "",
        },
    )


@administrator_required
@require_GET
def user_detail(request, pk):
    person = get_object_or_404(User, pk=pk, is_superuser=False)
    return render(
        request,
        "core/user_detail.html",
        {
            "person": person,
            "title": person.display_name,
            "accesses": UnitAccess.objects.filter(user=person).select_related("unit"),
        },
    )


@administrator_required
@require_http_methods(["POST"])
def approve_user(request, pk):
    with transaction.atomic():
        lock_user_management()
        person = get_object_or_404(User.objects.select_for_update(), pk=pk, is_superuser=False)
        if person.registration_status == User.RegistrationStatus.APPROVED:
            messages.info(request, "Este cadastro já está aprovado.")
            return redirect("user_detail", pk=person.pk)
        before = snapshot(person)
        person.registration_status = User.RegistrationStatus.APPROVED
        person.is_active = True
        person.session_version += 1
        person.save(update_fields=["registration_status", "is_active", "session_version"])
        audit(
            request.user,
            person,
            "registration_approved",
            before,
            description=f"Cadastro aprovado: {person.display_name}",
        )
    messages.success(request, "Cadastro aprovado. Agora configure o acesso às unidades.")
    return redirect("user_detail", pk=person.pk)


@administrator_required
@require_http_methods(["POST"])
def reject_user(request, pk):
    with transaction.atomic():
        lock_user_management()
        person = get_object_or_404(User.objects.select_for_update(), pk=pk, is_superuser=False)
        if (
            person.pk == request.user.pk
            or person.registration_status != User.RegistrationStatus.PENDING
        ):
            raise PermissionDenied
        before = snapshot(person)
        person.registration_status = User.RegistrationStatus.REJECTED
        person.is_active = False
        person.session_version += 1
        person.save(update_fields=["registration_status", "is_active", "session_version"])
        audit(
            request.user,
            person,
            "registration_rejected",
            before,
            description=f"Cadastro recusado: {person.display_name}",
        )
    messages.success(request, "Cadastro recusado e conta mantida inativa.")
    return redirect("user_detail", pk=person.pk)


@login_required
@require_http_methods(["GET", "POST"])
def employee_preview(request):
    if not request.user.has_administrator_role:
        raise PermissionDenied
    form = EmployeePreviewForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        request.session["employee_preview_unit_id"] = form.cleaned_data["unit"].pk
        messages.info(
            request,
            f"Visualização de funcionário ativada para {form.cleaned_data['unit'].name}.",
        )
        return redirect("dashboard")
    return render(
        request,
        "core/form.html",
        {
            "title": "Visualizar como funcionário",
            "intro": "Escolha uma unidade. Durante a simulação, as telas ficam somente para consulta.",
            "form": form,
            "back_url": reverse("dashboard"),
        },
    )


@login_required
@require_http_methods(["POST"])
def employee_preview_exit(request):
    if not request.user.has_administrator_role:
        raise PermissionDenied
    request.session.pop("employee_preview_unit_id", None)
    messages.success(request, "Visualização de funcionário encerrada.")
    return redirect("dashboard")


@administrator_required
@require_http_methods(["GET", "POST"])
def access_edit(request, user_pk, pk=None):
    person = get_object_or_404(User, pk=user_pk, is_superuser=False)
    with transaction.atomic():
        lock_user_management()
        obj = (
            get_object_or_404(UnitAccess.objects.select_for_update(), pk=pk, user=person)
            if pk
            else UnitAccess(user=person)
        )
        before = snapshot(obj) if pk else {}
        form = AccessForm(request.POST if request.method == "POST" else None, instance=obj)
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    access = form.save()
                    person.session_version += 1
                    person.save(update_fields=["session_version"])
                    audit(
                        request.user,
                        access,
                        "updated" if pk else "created",
                        before,
                        unit=access.unit,
                        description=f"Acesso de {person.display_name}: {access.unit.name}",
                    )
                if person.pk == request.user.pk:
                    update_session_auth_hash(request, person)
                messages.success(
                    request, "Acesso salvo. As sessões anteriores desse usuário foram encerradas."
                )
                return redirect("user_detail", pk=person.pk)
            except IntegrityError:
                form.add_error(None, "Já existe acesso a esta unidade. Edite o vínculo existente.")
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": f"Acesso de {person.display_name}",
            "back_url": reverse("user_detail", args=[person.pk]),
        },
    )


@administrator_required
@require_http_methods(["GET", "POST"])
def reset_password(request, pk):
    with transaction.atomic():
        lock_user_management()
        person = get_object_or_404(User.objects.select_for_update(), pk=pk, is_superuser=False)
        if person.pk == request.user.pk:
            return redirect("password_change")
        form = ResetUserPasswordForm(person, request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            person = form.save(commit=False)
            person.must_change_password = True
            person.temporary_password_expires_at = timezone.now() + timedelta(hours=24)
            person.session_version += 1
            person.save()
            audit(
                request.user,
                person,
                "password_reset",
                description=f"Senha redefinida: {person.display_name}",
            )
            messages.success(request, "Senha temporária definida. Ela expira em 24 horas.")
            return redirect("user_detail", pk=person.pk)
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": "Redefinir senha",
            "back_url": reverse("user_detail", args=[pk]),
            "intro": f"Defina uma senha temporária para {person.display_name}.",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def password_change(request):
    with transaction.atomic():
        person = User.objects.select_for_update().get(pk=request.user.pk)
        form = PasswordChangeForm(person, request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            person = form.save(commit=False)
            person.must_change_password = False
            person.temporary_password_expires_at = None
            person.save()
            audit(person, person, "password_changed", description="Senha alterada pelo usuário")
            update_session_auth_hash(request, person)
            messages.success(request, "Sua senha foi alterada.")
            return redirect("dashboard")
    return render(
        request,
        "core/form.html",
        {
            "form": form,
            "title": "Defina sua senha" if person.must_change_password else "Alterar senha",
            "intro": "Use pelo menos 8 caracteres. Sua senha é individual.",
            "back_url": "" if person.must_change_password else reverse("dashboard"),
        },
    )


def visible_events(user):
    if user.is_administrator:
        return AuditEvent.objects.all()
    managed = UnitAccess.objects.filter(
        user=user, is_active=True, unit__is_active=True, role=UnitAccess.Role.MANAGER
    ).values("unit_id")
    # A manager sees unit configuration events, never account/access changes.
    return AuditEvent.objects.filter(unit_id__in=managed, entity__in=["unitfood", "stockmovement"])


AUDIT_ACTION_LABELS = {
    "created": "Cadastro criado",
    "updated": "Cadastro atualizado",
    "xml_uploaded": "XML de NF-e recebido",
    "pdf_uploaded": "PDF de NF-e recebido",
    "fiscal_draft_saved": "Rascunho de NF-e salvo",
    "fiscal_draft_deleted": "Rascunho de NF-e excluído",
    "fiscal_suggestions_refreshed": "Sugestões da NF-e atualizadas",
    "fiscal_imported": "Entrada da NF-e confirmada",
    "fiscal_document_deleted": "NF-e excluída para reenvio",
    "created_from_fiscal_review": "Alimento criado pela NF-e",
    "pnae_exception_requested": "Exceção PNAE solicitada",
    "pnae_exception_approved": "Exceção PNAE aprovada",
    "pnae_import_completed": "Catálogo PNAE importado",
    "pnae_measure_corrected": "Medida PNAE corrigida",
    "movement_created": "Movimentação criada",
    "movement_reversed": "Movimentação estornada",
    "menu_uploaded": "Cardápio em PDF enviado",
    "menu_item_created": "Prato adicionado ao cardápio",
    "menu_item_updated": "Prato do cardápio atualizado",
    "menu_item_deleted": "Prato removido do cardápio",
    "disposal_created": "Descarte registrado",
    "registration_requested": "Cadastro solicitado",
    "registration_approved": "Cadastro aprovado",
    "registration_rejected": "Cadastro recusado",
    "password_changed": "Senha alterada",
    "password_reset": "Senha redefinida",
}

AUDIT_ENTITY_LABELS = {
    "unit": "Unidade",
    "category": "Categoria",
    "food": "Alimento",
    "presentation": "Embalagem",
    "pnaecatalog": "Catálogo PNAE",
    "pnaecatalogitem": "Item do Catálogo PNAE",
    "unitfood": "Estoque da unidade",
    "user": "Usuário",
    "unitaccess": "Acesso à unidade",
    "fiscaldocument": "NF-e",
    "fiscalpdf": "PDF da NF-e",
    "fiscalpnaeexception": "Exceção PNAE",
    "stockmovement": "Movimentação",
    "menudocument": "Cardápio em PDF",
    "weeklymenuitem": "Planejamento do cardápio",
    "disposalrecord": "Descarte",
}


@login_required
@require_GET
def audit_list(request):
    base_events = visible_events(request.user)
    action_values = list(base_events.order_by().values_list("action", flat=True).distinct())
    entity_values = list(base_events.order_by().values_list("entity", flat=True).distinct())
    actor_choices = list(
        base_events.filter(actor__isnull=False)
        .order_by("actor_name", "actor_id")
        .values("actor_id", "actor_name")
        .distinct()
    )
    unit_choices = list(
        base_events.filter(unit__isnull=False)
        .order_by("unit__code", "unit_id")
        .values("unit_id", "unit__code", "unit__name")
        .distinct()
    )
    events = base_events.select_related("unit")
    query = request.GET.get("q", "").strip()[:120]
    if query:
        events = events.filter(
            Q(description__icontains=query)
            | Q(actor_name__icontains=query)
            | Q(action__icontains=query)
            | Q(entity__icontains=query)
            | Q(entity_id__icontains=query)
        )
    actor = request.GET.get("actor", "")
    if actor == "system":
        events = events.filter(actor__isnull=True)
    elif actor.isdigit():
        events = events.filter(actor_id=actor)
    unit = request.GET.get("unit", "")
    if unit == "global":
        events = events.filter(unit__isnull=True)
    elif unit.isdigit():
        events = events.filter(unit_id=unit)
    action = request.GET.get("action", "")
    if action in action_values:
        events = events.filter(action=action)
    entity = request.GET.get("entity", "")
    if entity in entity_values:
        events = events.filter(entity=entity)
    date_from = parse_date(request.GET.get("date_from", ""))
    date_to = parse_date(request.GET.get("date_to", ""))
    if date_from:
        events = events.filter(created_at__date__gte=date_from)
    if date_to:
        events = events.filter(created_at__date__lte=date_to)

    query_params = request.GET.copy()
    query_params.pop("page", None)
    page = paginate(request, events)
    return render(
        request,
        "core/audit.html",
        {
            "title": "Auditoria",
            "page": page,
            "actor_choices": actor_choices,
            "unit_choices": unit_choices,
            "has_global_events": base_events.filter(unit__isnull=True).exists(),
            "action_choices": [
                (value, AUDIT_ACTION_LABELS.get(value, value.replace("_", " ").capitalize()))
                for value in sorted(action_values)
            ],
            "entity_choices": [
                (value, AUDIT_ENTITY_LABELS.get(value, value.replace("_", " ").capitalize()))
                for value in sorted(entity_values)
            ],
            "has_filters": any(
                request.GET.get(name)
                for name in ("q", "actor", "unit", "action", "entity", "date_from", "date_to")
            ),
            "pagination_query": query_params.urlencode(),
        },
    )


@login_required
@require_GET
def audit_detail(request, pk):
    event = get_object_or_404(visible_events(request.user).select_related("unit"), pk=pk)
    labels = {
        "code": "Código",
        "name": "Nome",
        "fiscal_address_match": "Referência do endereço na NF-e",
        "is_active": "Ativo",
        "category_id": "Categoria (ID)",
        "base_unit": "Medida",
        "requires_expiry": "Exigir validade",
        "food_id": "Alimento (ID)",
        "base_quantity": "Conteúdo da embalagem",
        "unit_id": "Unidade (ID)",
        "minimum": "Mínimo",
        "username": "Usuário",
        "first_name": "Nome",
        "last_name": "Sobrenome",
        "email": "E-mail",
        "is_general_admin": "Administrador geral",
        "registration_status": "Situação do cadastro",
        "user_id": "Usuário (ID)",
        "role": "Perfil",
        "access_key": "Chave de acesso",
        "number": "Número da NF-e",
        "series": "Série",
        "supplier_cnpj": "CNPJ do emitente",
        "supplier_name": "Emitente",
        "total": "Total da NF-e",
        "status": "Situação da NF-e",
        "review_draft": "Conteúdo do rascunho",
        "draft_saved_by_id": "Salvo por (ID)",
        "draft_saved_by_name": "Salvo por",
        "draft_saved_at": "Salvo em",
        "kind": "Tipo de movimentação",
        "occurred_at": "Data e hora",
        "reason": "Motivo",
        "fiscal_document_id": "NF-e (ID)",
        "reversed_movement_id": "Movimentação estornada (ID)",
    }

    def display_value(key, value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        if isinstance(value, bool):
            return "Sim" if value else "Não"
        if key in ("minimum", "base_quantity", "quantity", "total") and value != "—":
            # JSON keeps exact decimal strings; only the presentation is localized.
            return str(value).replace(".", ",")
        if key == "role":
            return dict(UnitAccess.Role.choices).get(value, value)
        if key == "status":
            return dict(FiscalDocument.Status.choices).get(value, value)
        if key == "registration_status":
            return dict(User.RegistrationStatus.choices).get(value, value)
        if key == "kind":
            return dict(StockMovement.Kind.choices).get(value, value)
        return value

    changes = [
        {
            "field": labels.get(key, key),
            "before": display_value(key, event.before.get(key, "—")),
            "after": display_value(key, event.after.get(key, "—")),
        }
        for key in sorted(event.before.keys() | event.after.keys())
        if event.before.get(key) != event.after.get(key)
    ]
    return render(
        request,
        "core/audit_detail.html",
        {
            "title": "Detalhe da alteração",
            "event": event,
            "changes": changes,
        },
    )
