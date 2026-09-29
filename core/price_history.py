from decimal import ROUND_HALF_UP, Decimal

from .fiscal_services import visible_fiscal_documents
from .models import FiscalDocumentItem

MONEY = Decimal("0.01")
PRICE_PRECISION = Decimal("0.0001")


def _money(value):
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _chart(entries):
    if not entries:
        return None

    width = Decimal("760")
    height = Decimal("280")
    left = Decimal("72")
    right = Decimal("24")
    top = Decimal("24")
    bottom = Decimal("44")
    plot_width = width - left - right
    plot_height = height - top - bottom

    timestamps = [Decimal(str(entry["issued_at"].timestamp())) for entry in entries]
    prices = [entry["price_per_base_unit"] for entry in entries]
    first_timestamp = min(timestamps)
    last_timestamp = max(timestamps)
    minimum = min(prices)
    maximum = max(prices)

    if minimum == maximum:
        padding = max(abs(minimum) * Decimal("0.1"), Decimal("1"))
    else:
        padding = (maximum - minimum) * Decimal("0.12")
    lower = max(Decimal("0"), minimum - padding)
    upper = maximum + padding
    price_span = upper - lower or Decimal("1")
    time_span = last_timestamp - first_timestamp

    points = []
    for index, (entry, timestamp) in enumerate(zip(entries, timestamps, strict=True)):
        x = (
            left + (timestamp - first_timestamp) / time_span * plot_width
            if time_span
            else left + plot_width / 2
        )
        y = top + (upper - entry["price_per_base_unit"]) / price_span * plot_height
        points.append(
            {
                "x": f"{x:.2f}",
                "y": f"{y:.2f}",
                "status": entry["status"],
                "tooltip": (
                    f"{entry['issued_at']:%d/%m/%Y} — "
                    f"R$ {_money(entry['price_per_base_unit'])} por "
                    f"{entry['base_unit']} — NF-e {entry['number']}"
                ),
                "is_latest": index == len(entries) - 1,
                "label_anchor": "end" if x > width - Decimal("120") else "start",
                "label_x": f"{x - 8 if x > width - Decimal('120') else x + 8:.2f}",
                "label_y": f"{max(top + Decimal('12'), y - Decimal('10')):.2f}",
                "label": f"R$ {_money(entry['price_per_base_unit'])}",
            }
        )

    ticks = []
    for index in range(5):
        ratio = Decimal(index) / Decimal("4")
        value = upper - price_span * ratio
        y = top + plot_height * ratio
        ticks.append({"value": _money(value), "y": f"{y:.2f}"})

    return {
        "width": int(width),
        "height": int(height),
        "left": int(left),
        "right_edge": int(width - right),
        "top": int(top),
        "bottom_edge": int(height - bottom),
        "polyline": " ".join(f"{point['x']},{point['y']}" for point in points),
        "points": points,
        "ticks": ticks,
        "first_date": entries[0]["issued_at"],
        "last_date": entries[-1]["issued_at"],
        "single_date": entries[0]["issued_at"].date() == entries[-1]["issued_at"].date(),
    }


def purchase_price_history(food, user):
    visible_documents = visible_fiscal_documents(user).values("pk")
    items = (
        FiscalDocumentItem.objects.filter(
            food=food,
            document_id__in=visible_documents,
            stock_quantity__gt=0,
        )
        .select_related("document")
        .order_by("document__issued_at", "pk")
    )

    entries = []
    for item in items:
        price_per_base_unit = (item.line_total / item.stock_quantity).quantize(
            PRICE_PRECISION, rounding=ROUND_HALF_UP
        )
        entries.append(
            {
                "issued_at": item.document.issued_at,
                "number": item.document.number,
                "document_id": item.document_id,
                "supplier_name": item.document.supplier_name,
                "status": item.document.status,
                "status_label": item.document.get_status_display(),
                "commercial_quantity": item.quantity,
                "commercial_unit": item.commercial_unit,
                "commercial_unit_price": item.unit_price,
                "stock_quantity": item.stock_quantity,
                "base_unit": food.base_unit,
                "line_total": item.line_total,
                "price_per_base_unit": price_per_base_unit,
            }
        )

    if not entries:
        return {"entries": [], "chart": None}

    first_price = entries[0]["price_per_base_unit"]
    latest_price = entries[-1]["price_per_base_unit"]
    variation = None
    if first_price:
        variation = ((latest_price - first_price) / first_price * Decimal("100")).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )

    return {
        "entries": list(reversed(entries)),
        "chart": _chart(entries),
        "latest_price": latest_price,
        "minimum_price": min(entry["price_per_base_unit"] for entry in entries),
        "maximum_price": max(entry["price_per_base_unit"] for entry in entries),
        "variation": variation,
    }
