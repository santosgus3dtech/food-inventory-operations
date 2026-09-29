import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .models import Food, normalize_name

NFE_NAMESPACE = "http://www.portalfiscal.inf.br/nfe"
DS_NAMESPACE = "http://www.w3.org/2000/09/xmldsig#"
NFE = f"{{{NFE_NAMESPACE}}}"
MAX_XML_SIZE = 2 * 1024 * 1024
MAX_ITEMS = 500


class NFeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedNFeItem:
    line_number: int
    supplier_code: str
    ean: str
    description: str
    ncm: str
    cfop: str
    commercial_unit: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class ParsedNFe:
    access_key: str
    number: str
    series: str
    issued_at: datetime
    delivery_date: date | None
    protocol: str
    authorization_received_at: datetime | None
    supplier_cnpj: str
    supplier_name: str
    recipient_cnpj: str
    recipient_name: str
    delivery_address: str
    total: Decimal
    payments: list[dict]
    warnings: list[str]
    signature_present: bool
    xml_sha256: str
    xml_content: bytes
    items: list[ParsedNFeItem]


def _text(element, tag, label, required=True, max_length=None):
    node = element.find(f"{NFE}{tag}") if element is not None else None
    value = " ".join((node.text or "").split()) if node is not None else ""
    if required and not value:
        raise NFeValidationError(f"O XML não informa {label}.")
    if max_length and len(value) > max_length:
        raise NFeValidationError(f"O campo {label} excede o tamanho permitido.")
    return value


def _decimal(value, label):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise NFeValidationError(f"O valor de {label} é inválido.") from exc
    if not number.is_finite():
        raise NFeValidationError(f"O valor de {label} é inválido.")
    return number


def _datetime(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NFeValidationError(f"A data de {label} é inválida.") from exc
    if parsed.tzinfo is None:
        raise NFeValidationError(f"A data de {label} precisa informar o fuso horário.")
    return parsed


def _date(value, label):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NFeValidationError(f"A data de {label} é inválida.") from exc


def access_key_check_digit(key):
    total = sum(
        int(digit) * (2, 3, 4, 5, 6, 7, 8, 9)[index % 8]
        for index, digit in enumerate(reversed(key[:-1]))
    )
    remainder = total % 11
    return 0 if remainder in (0, 1) else 11 - remainder


def parse_nfe_xml(content):
    if not content:
        raise NFeValidationError("O arquivo XML está vazio.")
    if len(content) > MAX_XML_SIZE:
        raise NFeValidationError("O XML ultrapassa o limite de 2 MB.")
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise NFeValidationError("O arquivo não é um XML seguro e bem formado.") from exc
    if root.tag != f"{NFE}nfeProc" or root.get("versao") != "4.00":
        raise NFeValidationError("Envie uma NF-e processada na versão 4.00.")

    inf_nfe = root.find(f"{NFE}NFe/{NFE}infNFe")
    protocol = root.find(f"{NFE}protNFe/{NFE}infProt")
    if inf_nfe is None or protocol is None:
        raise NFeValidationError("O XML não contém a NF-e e o protocolo de autorização.")
    if _text(protocol, "cStat", "situação da autorização") != "100":
        reason = _text(protocol, "xMotivo", "motivo da autorização", required=False)
        raise NFeValidationError(
            f"A NF-e não está autorizada (situação: {reason or 'desconhecida'})."
        )
    if _text(protocol, "tpAmb", "ambiente") != "1":
        raise NFeValidationError("NF-e de homologação não pode alterar o estoque de produção.")

    access_key = _text(protocol, "chNFe", "chave de acesso")
    if not re.fullmatch(r"\d{44}", access_key):
        raise NFeValidationError("A chave de acesso da NF-e é inválida.")
    if int(access_key[-1]) != access_key_check_digit(access_key):
        raise NFeValidationError("O dígito verificador da chave de acesso é inválido.")
    if inf_nfe.get("Id") != f"NFe{access_key}":
        raise NFeValidationError("A chave do protocolo não corresponde ao conteúdo da NF-e.")

    identification = inf_nfe.find(f"{NFE}ide")
    issuer = inf_nfe.find(f"{NFE}emit")
    recipient = inf_nfe.find(f"{NFE}dest")
    totals = inf_nfe.find(f"{NFE}total/{NFE}ICMSTot")
    if any(value is None for value in (identification, issuer, recipient, totals)):
        raise NFeValidationError("A NF-e não contém todos os grupos obrigatórios.")

    number = _text(identification, "nNF", "número da NF-e", max_length=20)
    series = _text(identification, "serie", "série da NF-e", max_length=10)
    model = _text(identification, "mod", "modelo do documento")
    issued_at = _datetime(_text(identification, "dhEmi", "emissão"), "emissão")
    supplier_cnpj = _text(issuer, "CNPJ", "CNPJ do emitente", max_length=14)
    recipient_cnpj = _text(recipient, "CNPJ", "CNPJ do destinatário", max_length=14)
    if not re.fullmatch(r"\d{14}", supplier_cnpj) or not re.fullmatch(r"\d{14}", recipient_cnpj):
        raise NFeValidationError("O CNPJ do emitente ou do destinatário é inválido.")
    if model != "55" or access_key[20:22] != "55":
        raise NFeValidationError("O arquivo não corresponde a uma NF-e modelo 55.")
    if access_key[6:20] != supplier_cnpj:
        raise NFeValidationError("O CNPJ do emitente não corresponde à chave de acesso.")
    if not number.isdigit() or int(access_key[25:34]) != int(number):
        raise NFeValidationError("O número da NF-e não corresponde à chave de acesso.")
    if not series.isdigit() or int(access_key[22:25]) != int(series):
        raise NFeValidationError("A série da NF-e não corresponde à chave de acesso.")
    if access_key[2:6] != issued_at.strftime("%y%m"):
        raise NFeValidationError("A data de emissão não corresponde à chave de acesso.")

    items = []
    for detail in inf_nfe.findall(f"{NFE}det"):
        product = detail.find(f"{NFE}prod")
        try:
            line_number = int(detail.get("nItem", ""))
        except ValueError as exc:
            raise NFeValidationError("A numeração de um item da NF-e é inválida.") from exc
        quantity = _decimal(_text(product, "qCom", "quantidade do item"), "quantidade do item")
        unit_price = _decimal(
            _text(product, "vUnCom", "preço unitário do item"), "preço unitário do item"
        )
        line_total = _decimal(_text(product, "vProd", "total do item"), "total do item")
        if quantity <= 0 or unit_price < 0 or line_total < 0:
            raise NFeValidationError("Os itens precisam ter quantidades e valores válidos.")
        items.append(
            ParsedNFeItem(
                line_number=line_number,
                supplier_code=_text(product, "cProd", "código do produto", max_length=60),
                ean=_text(product, "cEAN", "EAN", required=False, max_length=20),
                description=_text(product, "xProd", "descrição do produto", max_length=240),
                ncm=_text(product, "NCM", "NCM", required=False, max_length=10),
                cfop=_text(product, "CFOP", "CFOP", required=False, max_length=6),
                commercial_unit=_text(product, "uCom", "unidade comercial", max_length=12).upper(),
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    if not items:
        raise NFeValidationError("A NF-e não contém produtos.")
    if len(items) > MAX_ITEMS:
        raise NFeValidationError(f"A NF-e ultrapassa o limite de {MAX_ITEMS} itens.")
    if len({item.line_number for item in items}) != len(items):
        raise NFeValidationError("A NF-e contém números de item repetidos.")

    declared_product_total = _decimal(
        _text(totals, "vProd", "total dos produtos"), "total dos produtos"
    )
    if sum((item.line_total for item in items), Decimal("0")) != declared_product_total:
        raise NFeValidationError("A soma dos itens não corresponde ao total de produtos da NF-e.")
    invoice_total = _decimal(_text(totals, "vNF", "total da NF-e"), "total da NF-e")
    if invoice_total < 0:
        raise NFeValidationError("O total da NF-e não pode ser negativo.")

    payments = []
    for payment in inf_nfe.findall(f"{NFE}pag/{NFE}detPag"):
        payments.append(
            {
                "type": _text(payment, "tPag", "tipo de pagamento", max_length=3),
                "description": _text(
                    payment, "xPag", "descrição do pagamento", required=False, max_length=100
                ),
                "value": str(_decimal(_text(payment, "vPag", "pagamento"), "pagamento")),
            }
        )

    warnings = []
    change = _decimal(
        _text(inf_nfe.find(f"{NFE}pag"), "vTroco", "troco", required=False) or "0", "troco"
    )
    if payments:
        paid = sum((Decimal(payment["value"]) for payment in payments), Decimal("0")) - change
        if paid != invoice_total:
            warnings.append("A soma dos pagamentos, descontado o troco, difere do total da NF-e.")

    address = recipient.find(f"{NFE}enderDest")
    address_parts = [
        _text(address, "xLgr", "logradouro", required=False),
        _text(address, "nro", "número", required=False),
        _text(address, "xCpl", "complemento", required=False),
        _text(address, "xBairro", "bairro", required=False),
        _text(address, "xMun", "município", required=False),
        _text(address, "UF", "UF", required=False),
    ]
    signature_present = root.find(f"{NFE}NFe/{{{DS_NAMESPACE}}}Signature") is not None
    if not signature_present:
        warnings.append("O XML não contém assinatura digital.")

    authorization_received = _text(protocol, "dhRecbto", "data da autorização", required=False)
    return ParsedNFe(
        access_key=access_key,
        number=number,
        series=series,
        issued_at=issued_at,
        delivery_date=_date(
            _text(identification, "dPrevEntrega", "entrega", required=False), "entrega"
        ),
        protocol=_text(protocol, "nProt", "protocolo", max_length=30),
        authorization_received_at=(
            _datetime(authorization_received, "autorização") if authorization_received else None
        ),
        supplier_cnpj=supplier_cnpj,
        supplier_name=_text(issuer, "xNome", "nome do emitente", max_length=180),
        recipient_cnpj=recipient_cnpj,
        recipient_name=_text(recipient, "xNome", "nome do destinatário", max_length=180),
        delivery_address=" — ".join(part for part in address_parts if part)[:300],
        total=invoice_total,
        payments=payments,
        warnings=warnings,
        signature_present=signature_present,
        xml_sha256=hashlib.sha256(content).hexdigest(),
        xml_content=content,
        items=items,
    )


def nfe_measure(value):
    normalized = normalize_name(value).upper()
    if normalized in {"KG", "KILO", "QUILO"}:
        return Food.Measure.KG
    if normalized in {"L", "LT", "LITRO", "LITROS"}:
        return Food.Measure.LITRE
    if normalized in {"UN", "UND", "UNID", "UNIDADE", "PC", "PÇ"}:
        return Food.Measure.UNIT
    return None


def suggest_unit(delivery_address, units):
    normalized_address = normalize_name(delivery_address)
    matches = []
    for unit in units:
        reference = normalize_name(unit.fiscal_address_match or unit.name)
        if len(reference) >= 3 and reference in normalized_address:
            matches.append(unit)
    return matches[0] if len(matches) == 1 else None


def _food_words(value):
    normalized = normalize_name(value)
    replacements = {
        "fgo": "frango",
        "acuc": "acucar",
        "ac": "acucar",
        "bco": "branco",
        "esp": "espaguete",
        "ext": "extrato",
        "int": "instantaneo",
        "mac": "macarrao",
        "ref": "refinado",
        "sch": "",
        "semo": "",
        "seca": "sopa",
        "tom": "tomate",
        "cong": "",
        "congelado": "",
        "env": "",
        "resf": "",
        "pct": "",
        "pcte": "",
        "pacote": "",
        "fd": "",
        "fardo": "",
        "cx": "",
        "caixa": "",
        "tipo": "",
    }
    ignored = {
        "de",
        "da",
        "do",
        "em",
        "kg",
        "quilo",
        "quilos",
        "l",
        "lt",
        "litro",
        "litros",
        "un",
        "und",
        "unid",
        "unidade",
        "para",
    }
    words = []
    for word in re.findall(r"[a-z0-9]+", normalized):
        word = replacements.get(word, word)
        if (
            word
            and word not in ignored
            and not word.isdigit()
            and len(word) > 1
            and not re.fullmatch(r"\d+(?:g|kg|ml|l)", word)
        ):
            words.append(word)
    return words


def suggest_food(description, commercial_unit, foods):
    measure = nfe_measure(commercial_unit)
    source_words = _food_words(description)
    source = " ".join(source_words)
    source_set = set(source_words)
    ranked = []
    for food in foods:
        target_words = _food_words(food.name)
        target_set = set(target_words)
        overlap = len(source_set & target_set) / max(1, len(source_set | target_set))
        containment = len(source_set & target_set) / max(1, len(target_set))
        sequence = SequenceMatcher(None, source, " ".join(target_words)).ratio()
        contained = bool(target_words) and target_set.issubset(source_set)
        score = (
            Decimal(str(overlap)) * Decimal("0.35")
            + Decimal(str(containment)) * Decimal("0.40")
            + Decimal(str(sequence)) * Decimal("0.20")
        )
        if contained:
            score += Decimal("0.12")
        if measure == food.base_unit:
            score += Decimal("0.08")
        elif measure:
            score -= Decimal("0.05")
        ranked.append((score, food))
    ranked.sort(key=lambda pair: (pair[0], pair[1].pk), reverse=True)
    if not ranked or ranked[0][0] < Decimal("0.48"):
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < Decimal("0.06"):
        return None
    return ranked[0][1]


def suggested_stock_quantity(item, food):
    if not food:
        return None
    measure = nfe_measure(item.commercial_unit)
    quantity = item.quantity
    if measure != food.base_unit:
        description = normalize_name(getattr(item, "description", ""))
        package = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)(?![a-z])", description)
        if not package:
            return None
        package_quantity = Decimal(package.group(1).replace(",", "."))
        package_unit = package.group(2)
        if food.base_unit == Food.Measure.KG and package_unit in {"kg", "g"}:
            factor = package_quantity if package_unit == "kg" else package_quantity / 1000
        elif food.base_unit == Food.Measure.LITRE and package_unit in {"l", "ml"}:
            factor = package_quantity if package_unit == "l" else package_quantity / 1000
        else:
            return None
        quantity *= factor
    rounded = quantity.quantize(Decimal("0.001"))
    return rounded if rounded == quantity else None
