from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .access import manageable_units
from .models import (
    Category,
    FiscalDocument,
    FiscalDocumentItem,
    FiscalPdf,
    FiscalPnaeException,
    Food,
    StockMovement,
    StockMovementItem,
    SupplierProductMapping,
)
from .nfe import suggest_food, suggest_unit, suggested_stock_quantity
from .pnae import approve_pnae_exceptions, ensure_pnae_compliance, sync_pnae_exception_requests
from .services import audit, snapshot
from .stock_services import create_stock_movement


def visible_fiscal_documents(user):
    if user.is_administrator:
        return FiscalDocument.objects.all()
    unit_ids = manageable_units(user).values("pk")
    return FiscalDocument.objects.filter(
        Q(unit_id__in=unit_ids)
        | Q(stock_movements__unit_id__in=unit_ids)
        | Q(created_by=user, status=FiscalDocument.Status.REVIEW)
    ).distinct()


def visible_fiscal_pdfs(user):
    if user.is_administrator:
        return FiscalPdf.objects.all()
    document_ids = visible_fiscal_documents(user).values("pk")
    return FiscalPdf.objects.filter(Q(document_id__in=document_ids) | Q(created_by=user)).distinct()


def link_pending_pdfs(document):
    FiscalPdf.objects.filter(
        document__isnull=True,
        access_key=document.access_key,
    ).update(document=document, status=FiscalPdf.Status.LINKED)


def _supplier_mapping_lookups(supplier_cnpj, items):
    supplier_codes = {item.supplier_code for item in items}
    mappings = {
        mapping.supplier_code: mapping.food
        for mapping in SupplierProductMapping.objects.filter(
            supplier_cnpj=supplier_cnpj,
            supplier_code__in=supplier_codes,
            food__is_active=True,
        ).select_related("food")
    }
    eans = {item.ean for item in items if item.ean and item.ean != "SEM GTIN"}
    ean_candidates = {}
    for mapping in SupplierProductMapping.objects.filter(
        ean__in=eans, food__is_active=True
    ).select_related("food"):
        ean_candidates.setdefault(mapping.ean, set()).add(mapping.food)
    ean_mappings = {
        ean: next(iter(candidates))
        for ean, candidates in ean_candidates.items()
        if len(candidates) == 1
    }
    return mappings, ean_mappings


def _suggest_item_food(item, foods, mappings, ean_mappings):
    return (
        mappings.get(item.supplier_code)
        or ean_mappings.get(item.ean)
        or suggest_food(item.description, item.commercial_unit, foods)
    )


def create_fiscal_document(parsed, actor):
    existing = FiscalDocument.objects.filter(
        Q(access_key=parsed.access_key) | Q(xml_sha256=parsed.xml_sha256)
    ).first()
    if existing:
        link_pending_pdfs(existing)
        return existing, False

    units = list(manageable_units(actor).order_by("code", "pk"))
    foods = list(Food.objects.filter(is_active=True).select_related("category"))
    mappings, ean_mappings = _supplier_mapping_lookups(parsed.supplier_cnpj, parsed.items)

    try:
        with transaction.atomic():
            document = FiscalDocument.objects.create(
                access_key=parsed.access_key,
                number=parsed.number,
                series=parsed.series,
                issued_at=parsed.issued_at,
                delivery_date=parsed.delivery_date,
                protocol=parsed.protocol,
                authorization_received_at=parsed.authorization_received_at,
                supplier_cnpj=parsed.supplier_cnpj,
                supplier_name=parsed.supplier_name,
                recipient_cnpj=parsed.recipient_cnpj,
                recipient_name=parsed.recipient_name,
                delivery_address=parsed.delivery_address,
                total=parsed.total,
                payments=parsed.payments,
                validation_warnings=parsed.warnings,
                signature_present=parsed.signature_present,
                xml_sha256=parsed.xml_sha256,
                xml_content=parsed.xml_content,
                unit=suggest_unit(parsed.delivery_address, units),
                created_by=actor,
            )
            rows = []
            for parsed_item in parsed.items:
                food = _suggest_item_food(parsed_item, foods, mappings, ean_mappings)
                rows.append(
                    FiscalDocumentItem(
                        document=document,
                        line_number=parsed_item.line_number,
                        supplier_code=parsed_item.supplier_code,
                        ean=parsed_item.ean,
                        description=parsed_item.description,
                        ncm=parsed_item.ncm,
                        cfop=parsed_item.cfop,
                        commercial_unit=parsed_item.commercial_unit,
                        quantity=parsed_item.quantity,
                        unit_price=parsed_item.unit_price,
                        line_total=parsed_item.line_total,
                        food=food,
                        stock_quantity=suggested_stock_quantity(parsed_item, food),
                    )
                )
            FiscalDocumentItem.objects.bulk_create(rows)
            audit(
                actor,
                document,
                "xml_uploaded",
                unit=document.unit,
                description=f"NF-e {document.number} recebida para conferência",
            )
            link_pending_pdfs(document)
            return document, True
    except IntegrityError:
        existing = FiscalDocument.objects.filter(
            Q(access_key=parsed.access_key) | Q(xml_sha256=parsed.xml_sha256)
        ).first()
        if existing:
            link_pending_pdfs(existing)
            return existing, False
        raise


@transaction.atomic
def refresh_fiscal_document_suggestions(document_id, actor):
    document = FiscalDocument.objects.select_for_update().get(pk=document_id)
    if document.status != FiscalDocument.Status.REVIEW:
        return 0
    items = list(
        FiscalDocumentItem.objects.select_for_update()
        .filter(document=document, food__isnull=True)
        .order_by("line_number")
    )
    if not items:
        return 0
    foods = list(Food.objects.filter(is_active=True).select_related("category"))
    mappings, ean_mappings = _supplier_mapping_lookups(document.supplier_cnpj, items)
    changed = 0
    for item in items:
        food = _suggest_item_food(item, foods, mappings, ean_mappings)
        if not food:
            continue
        item.food = food
        item.stock_quantity = suggested_stock_quantity(item, food)
        item.save(update_fields=["food", "stock_quantity"])
        changed += 1
    if changed:
        audit(
            actor,
            document,
            "fiscal_suggestions_refreshed",
            unit=document.unit,
            description=f"{changed} sugestão(ões) atualizada(s) na NF-e {document.number}",
        )
    return changed


def create_fiscal_pdf(parsed, original_name, actor):
    existing = FiscalPdf.objects.filter(pdf_sha256=parsed.pdf_sha256).first()
    if existing:
        if not visible_fiscal_pdfs(actor).filter(pk=existing.pk).exists():
            raise ValidationError("Este PDF já foi recebido em outro acesso.")
        return existing, False

    document = visible_fiscal_documents(actor).filter(access_key=parsed.access_key).first()
    if parsed.embedded_nfe:
        document, _ = create_fiscal_document(parsed.embedded_nfe, actor)

    safe_name = original_name.replace("\\", "/").split("/")[-1].strip()[:180]
    try:
        with transaction.atomic():
            fiscal_pdf = FiscalPdf.objects.create(
                access_key=parsed.access_key,
                original_name=safe_name or f"DANFE-{parsed.access_key}.pdf",
                page_count=parsed.page_count,
                pdf_sha256=parsed.pdf_sha256,
                pdf_content=parsed.pdf_content,
                validation_warnings=parsed.warnings,
                document=document,
                status=(FiscalPdf.Status.LINKED if document else FiscalPdf.Status.AWAITING_XML),
                created_by=actor,
            )
            audit(
                actor,
                fiscal_pdf,
                "pdf_uploaded",
                unit=document.unit if document else None,
                description=(
                    f"DANFE da NF-e {document.number} recebido"
                    if document
                    else f"DANFE {parsed.access_key} aguardando XML"
                ),
            )
            return fiscal_pdf, True
    except IntegrityError:
        existing = FiscalPdf.objects.filter(pdf_sha256=parsed.pdf_sha256).first()
        if existing and visible_fiscal_pdfs(actor).filter(pk=existing.pk).exists():
            return existing, False
        raise


@transaction.atomic
def create_food_from_fiscal_item(*, document, item, name, category, base_unit, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    document = FiscalDocument.objects.select_for_update().get(pk=document.pk)
    if document.status != FiscalDocument.Status.REVIEW:
        raise ValidationError("Esta NF-e já foi confirmada.")
    item = FiscalDocumentItem.objects.select_for_update().get(pk=item.pk, document=document)
    category = Category.objects.select_for_update().get(pk=category.pk, is_active=True)
    existing_codes = set(
        Food.objects.select_for_update()
        .filter(code__startswith="AL")
        .values_list("code", flat=True)
    )
    sequence = (
        max(
            (int(code[2:]) for code in existing_codes if code[2:].isdigit()),
            default=0,
        )
        + 1
    )
    code = f"AL{sequence:03d}"
    while code in existing_codes:
        sequence += 1
        code = f"AL{sequence:03d}"
    food = Food.objects.create(
        name=name,
        code=code,
        category=category,
        base_unit=base_unit,
        requires_expiry=False,
    )
    item.food = food
    parsed_item = type(
        "ParsedItemReference",
        (),
        {
            "commercial_unit": item.commercial_unit,
            "description": item.description,
            "quantity": item.quantity,
        },
    )()
    item.stock_quantity = suggested_stock_quantity(parsed_item, food)
    item.save(update_fields=["food", "stock_quantity"])
    SupplierProductMapping.objects.update_or_create(
        supplier_cnpj=document.supplier_cnpj,
        supplier_code=item.supplier_code,
        defaults={"ean": item.ean, "food": food},
    )
    audit(
        actor,
        food,
        "created_from_fiscal_review",
        description=f"{food.code} criado durante a conferência da NF-e {document.number}",
    )
    return food


def _validate_match_rows(document, matches, actor, *, require_complete):
    manageable = {
        unit.pk: unit for unit in manageable_units(actor).select_for_update().order_by("pk")
    }
    items = list(
        FiscalDocumentItem.objects.select_for_update()
        .filter(document=document)
        .order_by("line_number")
    )
    item_by_id = {item.pk: item for item in items}
    if len(matches) != len(items) or {match["item_id"] for match in matches} != set(item_by_id):
        raise ValidationError("A lista de itens da NF-e foi alterada. Recarregue a página.")

    selected_food_ids = {match["food"].pk for match in matches if match.get("food")}
    locked_foods = {
        food.pk: food
        for food in Food.objects.select_for_update().filter(
            pk__in=selected_food_ids, is_active=True
        )
    }
    if set(locked_foods) != selected_food_ids:
        raise ValidationError("Um dos alimentos selecionados foi desativado.")

    prepared = []
    for match in matches:
        item = item_by_id[match["item_id"]]
        food = match.get("food")
        total = match.get("stock_quantity")
        allocations = []
        for unit, quantity in match.get("allocations", []):
            if unit.pk not in manageable:
                raise PermissionDenied
            if quantity and quantity > 0:
                allocations.append((manageable[unit.pk], quantity))
        allocated_total = sum((quantity for _, quantity in allocations), 0)
        if total is not None and allocated_total > total:
            raise ValidationError(f"A distribuição de {item.description} ultrapassa o total.")
        if require_complete:
            if not food or total is None or not allocations or allocated_total != total:
                raise ValidationError(
                    f"Conclua a identificação e a distribuição de {item.description}."
                )
        if food:
            food = locked_foods[food.pk]
            if food.base_unit == Food.Measure.UNIT and (
                total is not None
                and total != total.to_integral_value()
                or any(value != value.to_integral_value() for _, value in allocations)
            ):
                raise ValidationError(f"{food.name} exige quantidades inteiras.")
        prepared.append(
            {
                **match,
                "item": item,
                "food": food,
                "allocations": allocations,
            }
        )
    return items, prepared


@transaction.atomic
def save_fiscal_document_draft(document_id, matches, actor):
    document = FiscalDocument.objects.select_for_update().get(pk=document_id)
    if document.status == FiscalDocument.Status.IMPORTED:
        return document, False
    _, prepared = _validate_match_rows(document, matches, actor, require_complete=False)
    draft_items = {}
    for row in prepared:
        draft_items[str(row["item"].pk)] = {
            "food_id": row["food"].pk if row["food"] else None,
            "stock_quantity": (
                str(row["stock_quantity"]) if row.get("stock_quantity") is not None else ""
            ),
            "lot_code": row.get("lot_code", ""),
            "expires_on": (row["expires_on"].isoformat() if row.get("expires_on") else ""),
            "allocations": {str(unit.pk): str(quantity) for unit, quantity in row["allocations"]},
            "pnae_exception_reason": row.get("pnae_exception_reason", ""),
        }
    sync_pnae_exception_requests(prepared, actor)
    document.review_draft = {"items": draft_items}
    document.draft_saved_by = actor
    document.draft_saved_at = timezone.now()
    document.save(update_fields=["review_draft", "draft_saved_by", "draft_saved_at"])
    audit(
        actor,
        document,
        "fiscal_draft_saved",
        unit=document.unit,
        description=f"Rascunho da NF-e {document.number} salvo sem alterar o estoque",
    )
    return document, True


@transaction.atomic
def approve_fiscal_document_pnae_exceptions(document_id, matches, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    document, _saved = save_fiscal_document_draft(document_id, matches, actor)
    if document.status == FiscalDocument.Status.IMPORTED:
        return document, 0
    _, prepared = _validate_match_rows(document, matches, actor, require_complete=False)
    approved = approve_pnae_exceptions(prepared, actor)
    return document, approved


@transaction.atomic
def delete_fiscal_document_draft(document_id, actor):
    document = FiscalDocument.objects.select_for_update().get(pk=document_id)
    if document.status == FiscalDocument.Status.IMPORTED:
        return document, False
    if not document.review_draft and not document.draft_saved_at:
        return document, False

    deleted_draft = {
        "review_draft": document.review_draft,
        "draft_saved_by_id": document.draft_saved_by_id,
        "draft_saved_by_name": (
            document.draft_saved_by.display_name if document.draft_saved_by else ""
        ),
        "draft_saved_at": (document.draft_saved_at.isoformat() if document.draft_saved_at else ""),
    }
    document.review_draft = {}
    document.draft_saved_by = None
    document.draft_saved_at = None
    document.save(update_fields=["review_draft", "draft_saved_by", "draft_saved_at"])
    audit(
        actor,
        document,
        "fiscal_draft_deleted",
        before=deleted_draft,
        after={
            "review_draft": {},
            "draft_saved_by_id": None,
            "draft_saved_by_name": "",
            "draft_saved_at": "",
        },
        unit=document.unit,
        description=f"Rascunho da NF-e {document.number} excluído",
    )
    return document, True


def _fiscal_document_deletion_snapshot(document):
    return {
        "document": {
            **snapshot(document),
            "xml_sha256": document.xml_sha256,
            "issued_at": document.issued_at.isoformat(),
            "created_at": document.created_at.isoformat(),
        },
        "items": [
            {
                "line_number": item.line_number,
                "supplier_code": item.supplier_code,
                "ean": item.ean,
                "description": item.description,
                "ncm": item.ncm,
                "cfop": item.cfop,
                "commercial_unit": item.commercial_unit,
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "line_total": str(item.line_total),
                "food_id": item.food_id,
                "stock_quantity": str(item.stock_quantity) if item.stock_quantity else "",
                "lot_code": item.lot_code,
                "expires_on": item.expires_on.isoformat() if item.expires_on else "",
            }
            for item in document.items.all()
        ],
        "pdfs": [
            {
                "access_key": pdf.access_key,
                "original_name": pdf.original_name,
                "page_count": pdf.page_count,
                "pdf_sha256": pdf.pdf_sha256,
                "status": pdf.status,
            }
            for pdf in document.pdfs.all()
        ],
        "pnae_exceptions": [
            {
                "item_id": exception.item_id,
                "catalog_id": exception.catalog_id,
                "food_id": exception.food_id,
                "reason": exception.reason,
                "status": exception.status,
            }
            for exception in FiscalPnaeException.objects.filter(item__document=document)
        ],
    }


@transaction.atomic
def delete_fiscal_documents_for_reimport(document_ids, actor):
    if not actor.is_administrator:
        raise PermissionDenied
    requested_ids = {int(document_id) for document_id in document_ids}
    documents = list(
        FiscalDocument.objects.select_for_update(of=("self",))
        .filter(pk__in=requested_ids)
        .select_related("unit")
        .prefetch_related("items", "pdfs")
        .order_by("pk")
    )
    if len(documents) != len(requested_ids):
        raise ValidationError("Uma ou mais NF-e não foram encontradas.")
    if StockMovement.objects.filter(fiscal_document_id__in=requested_ids).exists() or (
        StockMovementItem.objects.filter(
            fiscal_document_item__document_id__in=requested_ids
        ).exists()
    ):
        raise ValidationError(
            "Há NF-e vinculada a movimentações de estoque. Estorne as movimentações antes."
        )

    summaries = []
    for document in documents:
        deletion_snapshot = _fiscal_document_deletion_snapshot(document)
        item_count = len(deletion_snapshot["items"])
        pdf_count = len(deletion_snapshot["pdfs"])
        audit(
            actor,
            document,
            "fiscal_document_deleted",
            before=deletion_snapshot,
            after={},
            unit=document.unit,
            description=(
                f"NF-e {document.number} excluída para reenvio "
                f"({item_count} item(ns), {pdf_count} PDF(s))"
            ),
        )
        FiscalPnaeException.objects.filter(item__document=document).delete()
        FiscalPdf.objects.filter(document=document).delete()
        FiscalDocumentItem.objects.filter(document=document).delete()
        summaries.append(
            {
                "id": document.pk,
                "number": document.number,
                "access_key": document.access_key,
                "xml_sha256": document.xml_sha256,
                "items": item_count,
                "pdfs": pdf_count,
            }
        )
        document.delete()
    return summaries


@transaction.atomic
def confirm_fiscal_document(document_id, matches, actor):
    document = FiscalDocument.objects.select_for_update().get(pk=document_id)
    if document.status == FiscalDocument.Status.IMPORTED:
        return document, False
    items, prepared = _validate_match_rows(document, matches, actor, require_complete=True)
    ensure_pnae_compliance(prepared)

    lines_by_unit = {}
    for row in prepared:
        item = row["item"]
        food = row["food"]
        quantity = row["stock_quantity"]
        item.food = food
        item.stock_quantity = quantity
        item.lot_code = row.get("lot_code", "")
        item.expires_on = row.get("expires_on")
        item.full_clean()
        for unit, allocated_quantity in row["allocations"]:
            lines_by_unit.setdefault(unit, []).append(
                {
                    "food": food,
                    "quantity": allocated_quantity,
                    "lot_code": item.lot_code,
                    "expires_on": item.expires_on,
                    "fiscal_document_item": item,
                }
            )

    for unit in sorted(lines_by_unit, key=lambda candidate: candidate.pk):
        create_stock_movement(
            unit=unit,
            kind="receipt",
            lines=lines_by_unit[unit],
            actor=actor,
            reason=f"Recebimento da NF-e {document.number}",
            fiscal_document=document,
        )

    for item in items:
        item.save(update_fields=["food", "stock_quantity", "lot_code", "expires_on"])
        SupplierProductMapping.objects.update_or_create(
            supplier_cnpj=document.supplier_cnpj,
            supplier_code=item.supplier_code,
            defaults={"ean": item.ean, "food": item.food},
        )

    before = snapshot(document)
    allocated_units = sorted(lines_by_unit, key=lambda candidate: candidate.pk)
    document.unit = allocated_units[0] if len(allocated_units) == 1 else None
    document.status = FiscalDocument.Status.IMPORTED
    document.confirmed_by = actor
    document.confirmed_at = timezone.now()
    document.review_draft = {}
    document.draft_saved_by = None
    document.draft_saved_at = None
    document.save(
        update_fields=[
            "unit",
            "status",
            "confirmed_by",
            "confirmed_at",
            "review_draft",
            "draft_saved_by",
            "draft_saved_at",
        ]
    )
    audit(
        actor,
        document,
        "fiscal_imported",
        before,
        document.unit,
        description=(f"NF-e {document.number} confirmada em {len(allocated_units)} unidade(s)"),
    )
    return document, True
