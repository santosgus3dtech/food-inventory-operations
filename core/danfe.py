import hashlib
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .nfe import MAX_XML_SIZE, ParsedNFe, access_key_check_digit, parse_nfe_xml

MAX_PDF_SIZE = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_EXTRACTED_TEXT = 250_000


class DanfeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedDanfe:
    access_key: str
    page_count: int
    pdf_sha256: str
    pdf_content: bytes
    warnings: list[str]
    embedded_nfe: ParsedNFe | None


def _is_valid_access_key(value):
    return (
        len(value) == 44
        and value.isdigit()
        and value[20:22] == "55"
        and int(value[-1]) == access_key_check_digit(value)
    )


def _access_keys_from_text(text):
    candidates = set()
    patterns = (
        r"(?<!\d)\d{44}(?!\d)",
        r"(?<!\d)(?:\d{4}[ .\-]?){10}\d{4}(?!\d)",
    )
    for source in (text, " ".join(text.split())):
        for pattern in patterns:
            for match in re.findall(pattern, source):
                candidate = re.sub(r"\D", "", match)
                if _is_valid_access_key(candidate):
                    candidates.add(candidate)
    return candidates


def _embedded_nfes(reader):
    parsed = []
    for name, contents in list(reader.attachments.items())[:20]:
        if not name.lower().endswith(".xml"):
            continue
        for content in contents[:5]:
            if not content or len(content) > MAX_XML_SIZE:
                continue
            try:
                parsed.append(parse_nfe_xml(content))
            except ValueError:
                continue
    return parsed


def parse_danfe_pdf(content):
    if not content:
        raise DanfeValidationError("O arquivo PDF está vazio.")
    if len(content) > MAX_PDF_SIZE:
        raise DanfeValidationError("O PDF ultrapassa o limite de 10 MB.")
    if not content.startswith(b"%PDF-"):
        raise DanfeValidationError("O arquivo enviado não é um PDF válido.")

    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DanfeValidationError("PDF protegido por senha não pode ser processado.")
        page_count = len(reader.pages)
        if not 1 <= page_count <= MAX_PDF_PAGES:
            raise DanfeValidationError(f"O PDF deve ter entre 1 e {MAX_PDF_PAGES} páginas.")
        text_parts = []
        extracted_length = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            extracted_length += len(page_text)
            if extracted_length > MAX_EXTRACTED_TEXT:
                raise DanfeValidationError("O PDF contém texto demais para processamento seguro.")
            text_parts.append(page_text)
        embedded = _embedded_nfes(reader)
    except DanfeValidationError:
        raise
    except (PdfReadError, OSError, TypeError, ValueError) as exc:
        raise DanfeValidationError("Não foi possível ler a estrutura do PDF.") from exc

    embedded_keys = {item.access_key for item in embedded}
    if len(embedded_keys) > 1:
        raise DanfeValidationError("O PDF contém XMLs de mais de uma NF-e.")
    embedded_nfe = embedded[0] if embedded else None

    text = "\n".join(text_parts)
    text_keys = _access_keys_from_text(text)
    if embedded_nfe and text_keys and text_keys != {embedded_nfe.access_key}:
        raise DanfeValidationError("A chave impressa no PDF difere do XML embutido.")
    access_keys = embedded_keys or text_keys
    if len(access_keys) > 1:
        raise DanfeValidationError("O PDF apresenta mais de uma chave de acesso válida.")
    if not access_keys:
        raise DanfeValidationError(
            "Não foi possível identificar a chave de acesso. Envie um DANFE com texto "
            "pesquisável ou use o XML autorizado."
        )

    normalized_text = " ".join(text.upper().split())
    fiscal_markers = ("DANFE", "CHAVE DE ACESSO", "NOTA FISCAL ELETR")
    if not embedded_nfe and not any(marker in normalized_text for marker in fiscal_markers):
        raise DanfeValidationError("O PDF não parece ser um DANFE de NF-e.")

    warnings = []
    if embedded_nfe:
        warnings.append("O PDF contém o XML autorizado da NF-e e foi vinculado automaticamente.")
    else:
        warnings.append(
            "O PDF não contém o XML fiscal autorizado; envie o XML para conferir os itens."
        )
    return ParsedDanfe(
        access_key=next(iter(access_keys)),
        page_count=page_count,
        pdf_sha256=hashlib.sha256(content).hexdigest(),
        pdf_content=content,
        warnings=warnings,
        embedded_nfe=embedded_nfe,
    )
