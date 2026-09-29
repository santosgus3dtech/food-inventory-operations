import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_MENU_PDF_SIZE = 10 * 1024 * 1024
MAX_MENU_PDF_PAGES = 10
MAX_MENU_TEXT = 150_000

MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

MEAL_TYPES = {
    "colacao": "snack",
    "colacao bercario": "nursery_snack",
    "almoco": "lunch",
    "refeicao saida": "departure_meal",
    "sobremesa": "dessert",
}


class MenuValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedMenuMeal:
    service_date: date
    meal_type: str
    description: str


@dataclass(frozen=True)
class ParsedMenu:
    title: str
    week_start: date | None
    week_end: date | None
    nutritionist_name: str
    nutritionist_registration: str
    notes: str
    page_count: int
    pdf_sha256: str
    pdf_content: bytes
    source_text: str
    meals: tuple[ParsedMenuMeal, ...]
    warnings: tuple[str, ...]


def _normalize(value):
    ascii_value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).split())


def _clean_lines(value):
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _year_from(source):
    matches = re.findall(r"\b(20\d{2})\b", source)
    return int(matches[-1]) if matches else None


def _period_from(text, original_name):
    combined = _normalize(f"{original_name}\n{text}")
    year = _year_from(f"{original_name}\n{text}")
    if year is None:
        return None, None

    explicit = re.search(
        r"\b(\d{1,2}) de ([a-z]+) a (\d{1,2}) de ([a-z]+)(?: de 20\d{2})?\b",
        combined,
    )
    if explicit:
        start_day, start_month_name, end_day, end_month_name = explicit.groups()
        start_month = MONTHS.get(start_month_name)
        end_month = MONTHS.get(end_month_name)
    else:
        same_month = re.search(
            r"\b(\d{1,2}) a (\d{1,2}) de ([a-z]+)(?: de 20\d{2})?\b",
            combined,
        )
        if not same_month:
            return None, None
        start_day, end_day, month_name = same_month.groups()
        start_month = end_month = MONTHS.get(month_name)

    if not start_month or not end_month:
        return None, None
    start_year = year - 1 if start_month > end_month else year
    try:
        week_start = date(start_year, start_month, int(start_day))
        week_end = date(year, end_month, int(end_day))
    except ValueError:
        return None, None
    if week_end < week_start or week_end - week_start > timedelta(days=14):
        return None, None
    return week_start, week_end


def _nutritionist_from(text):
    match = re.search(
        r"(?im)^\s*([^\n]{2,80}?)\s*[–-]\s*CRN\s*([0-9./-]+)\s*$",
        text,
    )
    if not match:
        return "", ""
    return " ".join(match.group(1).split()), match.group(2).strip()


def _notes_from(text):
    match = re.search(r"(?is)\bObs\s*:\s*(.+)$", text)
    if not match:
        return ""
    lines = []
    for line in match.group(1).splitlines():
        cleaned = " ".join(line.replace("", "").split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)[:4000]


def _meals_from_tables(tables, week_start):
    if week_start is None:
        return []
    meals = {}
    for table in tables:
        header_index = next(
            (
                index
                for index, row in enumerate(table)
                if row
                and len(row) >= 6
                and _normalize(row[0]) == "horarios"
                and [_normalize(value) for value in row[1:6]]
                == ["segunda", "terca", "quarta", "quinta", "sexta"]
            ),
            None,
        )
        if header_index is None:
            continue
        for row in table[header_index + 1 :]:
            if not row or len(row) < 6:
                continue
            meal_type = MEAL_TYPES.get(_normalize(row[0]))
            if not meal_type:
                continue
            for day_offset, cell in enumerate(row[1:6]):
                description = _clean_lines(cell)
                if not description:
                    continue
                key = (week_start + timedelta(days=day_offset), meal_type)
                meals[key] = ParsedMenuMeal(
                    service_date=key[0],
                    meal_type=meal_type,
                    description=description[:4000],
                )
    return list(meals.values())


def parse_menu_pdf(content, original_name="cardapio.pdf"):
    if not content:
        raise MenuValidationError("O arquivo PDF está vazio.")
    if len(content) > MAX_MENU_PDF_SIZE:
        raise MenuValidationError("O cardápio ultrapassa o limite de 10 MB.")
    if not content.startswith(b"%PDF-"):
        raise MenuValidationError("O arquivo enviado não é um PDF válido.")

    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise MenuValidationError("PDF protegido por senha não pode ser processado.")
        page_count = len(reader.pages)
        if not 1 <= page_count <= MAX_MENU_PDF_PAGES:
            raise MenuValidationError(
                f"O cardápio deve ter entre 1 e {MAX_MENU_PDF_PAGES} páginas."
            )

        text_parts = []
        tables = []
        with pdfplumber.open(BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                if sum(len(value) for value in text_parts) > MAX_MENU_TEXT:
                    raise MenuValidationError(
                        "O PDF contém texto demais para processamento seguro."
                    )
                tables.extend(page.extract_tables())
    except MenuValidationError:
        raise
    except (PDFSyntaxError, PdfReadError, OSError, TypeError, ValueError) as exc:
        raise MenuValidationError("Não foi possível ler a estrutura do cardápio em PDF.") from exc

    source_text = "\n".join(text_parts).strip()
    week_start, week_end = _period_from(source_text, original_name)
    nutritionist_name, nutritionist_registration = _nutritionist_from(source_text)
    meals = _meals_from_tables(tables, week_start)
    warnings = []
    if not source_text:
        warnings.append("O PDF não possui texto pesquisável.")
    if week_start is None or week_end is None:
        warnings.append("O período da semana não pôde ser identificado automaticamente.")
    if not meals:
        warnings.append("A tabela de refeições não pôde ser identificada automaticamente.")
    elif len(meals) < 25:
        warnings.append(f"Foram identificadas {len(meals)} de 25 referências esperadas.")

    title_match = re.search(r"(?im)^\s*(Cardápio\s+Semanal)\s*$", source_text)
    return ParsedMenu(
        title=title_match.group(1) if title_match else "Cardápio semanal",
        week_start=week_start,
        week_end=week_end,
        nutritionist_name=nutritionist_name,
        nutritionist_registration=nutritionist_registration,
        notes=_notes_from(source_text),
        page_count=page_count,
        pdf_sha256=hashlib.sha256(content).hexdigest(),
        pdf_content=content,
        source_text=source_text[:MAX_MENU_TEXT],
        meals=tuple(sorted(meals, key=lambda item: (item.service_date, item.meal_type))),
        warnings=tuple(warnings),
    )
