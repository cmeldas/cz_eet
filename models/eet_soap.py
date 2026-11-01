# -*- coding: utf-8 -*-
"""Sestavení, podpis, odeslání a rozbor SOAP zpráv EET 2.0 (rozhraní Finanční správy v4.1).

Čisté funkce bez Odoo ORM, aby šly testovat samostatně. Podpis podle WS-Security 1.1 /
XML-DSig: podepisuje se právě element ``soap:Body`` (Exclusive C14N, SHA-256, RSA-SHA256),
certifikát poplatníka je v ``BinarySecurityToken``. Struktura kopíruje vzorové zprávy
Finanční správy (``CZ*.eet.v4.req.xml``) včetně ``InclusiveNamespaces``.
"""
import base64
import functools
import hashlib
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import pytz
import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_EET = "http://fs.gov.cz/eet/schema/v4"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_EC = "http://www.w3.org/2001/10/xml-exc-c14n#"

ALG_EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
ALG_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
ALG_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
BST_ENCODING = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
BST_VALUE_TYPE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3"

SOAP_ACTION = "http://fs.gov.cz/eet/OdeslaniTrzby"
URL_PLAYGROUND = "https://pg.trzbyeet.gov.cz/eet/services/EETServiceSOAP/v4"
URL_PRODUCTION = "https://trzbyeet.gov.cz/eet/services/EETServiceSOAP/v4"

TZ_PRAGUE = pytz.timezone("Europe/Prague")
MAX_MESSAGE_BYTES = 12 * 1024
DEFAULT_TIMEOUT = 5.0

RE_EIC = re.compile(r"^CZ[0-9]{8,10}$")
RE_ID_POKL = re.compile(r"^[0-9a-zA-Z\.,:;/#\-_ ]{1,20}$")
RE_PORAD_CIS = re.compile(r"^[0-9a-zA-Z\.,:;/#\-_ ]{1,25}$")
RE_POK = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}-[0-9a-fA-F]{2}$")
ALLOWED_CHARS = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,:;/#-_ ")

XSD_PATH = Path(__file__).resolve().parents[1] / "static" / "xsd" / "EETXMLSchema.xsd"

# Certifikáty CA EET z eet.gov.cz (dokumenty ke stažení): vydávající SubCA a kořenová CA
# pro každé prostředí. Když FS vydá novou SubCA (platí 4 roky), přidat sem její soubor.
CA_DIR = Path(__file__).resolve().parents[1] / "data" / "ca"
CA_FILES = {
    "production": ("ca_eet-sub_prod.pem", "ca_eet-root_prod.pem"),
    "playground": ("ca_eet-sub_cert-playground.crt", "ca_eet-root_cert-playground.crt"),
}


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _bool(value) -> str:
    return "true" if value else "false"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def format_amount(amount: float) -> str:
    """Částka přesně na 2 desetinná místa (``250.00``, ``-187.20``), bez ``-0.00``."""
    value = round(float(amount), 2)
    if abs(value) >= 100_000_000:
        raise ValueError(f"Částka {value} je mimo rozsah EET (±99 999 999,99).")
    text = f"{value:.2f}"
    return "0.00" if text == "-0.00" else text


def format_datetime(value: datetime) -> str:
    """ISO 8601 v místním čase ČR s povinnou zónou: ``2027-01-09T04:25:28+01:00``.

    Naivní datetime (Odoo ukládá v UTC) se bere jako UTC.
    """
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(TZ_PRAGUE).replace(microsecond=0).isoformat()


def parse_datetime(text: str | None) -> datetime | None:
    """ISO 8601 se zónou -> naivní UTC datetime pro pole Odoo."""
    if not text:
        return None
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is not None:
        value = value.astimezone(pytz.utc).replace(tzinfo=None)
    return value


def sanitize_identifier(value: str | None, max_length: int) -> str:
    """Odstraní diakritiku a nepovolené znaky (masky ``id_pokl`` / ``porad_cis``)."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch in ALLOWED_CHARS else " " for ch in text)
    text = " ".join(text.split())
    return text[:max_length].strip()


def new_uuid() -> str:
    return str(uuid.uuid4())


@functools.lru_cache(maxsize=1)
def _get_schema() -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(XSD_PATH)))


def xsd_errors(element: etree._Element) -> list[str]:
    """Seznam chyb proti ``EETXMLSchema.xsd``; prázdný seznam = validní."""
    schema = _get_schema()
    if schema.validate(element):
        return []
    return [entry.message for entry in schema.error_log]


def _issued_by(certificate: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        certificate.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


@functools.lru_cache(maxsize=1)
def _issuing_cas() -> dict:
    """Vydávající SubCA podle prostředí; jen ty, které podepsala kořenová CA téhož prostředí."""
    cas = {}
    for environment, (sub_file, root_file) in CA_FILES.items():
        sub = x509.load_pem_x509_certificate((CA_DIR / sub_file).read_bytes())
        root = x509.load_pem_x509_certificate((CA_DIR / root_file).read_bytes())
        if _issued_by(sub, root):
            cas[environment] = sub
    return cas


def certificate_environment(certificate_der: bytes) -> str | None:
    """Prostředí CA EET, která pokladní certifikát vydala (``production`` / ``playground``).

    ``None``, když ho nevydala žádná známá SubCA EET. Ověřuje se jen řetězec certifikátů;
    platnost v čase a zneplatnění (CRL) kontroluje server u každé zprávy.
    """
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except ValueError:
        return None
    for environment, sub in _issuing_cas().items():
        if _issued_by(certificate, sub):
            return environment
    return None


def build_trzba(values: dict) -> etree._Element:
    """Element ``<v4:Trzba>`` z připravených hodnot.

    Povinné klíče: ``uuid_zpravy``, ``dat_odesl``, ``prvni_zaslani``, ``eic_popl``,
    ``id_jednotky``, ``id_pokl``, ``porad_cis``, ``dat_trzby``, ``celk_trzba``.
    Nepovinné (vynechají se, když chybí nebo jsou ``None``/``False``): ``overeni``,
    ``eic_poverujiciho``, ``povereni_vice_popl``, ``urceno_cerp_zuct``, ``cerp_zuct``.
    Částky a časy už musí být řetězce ve formátu EET (``format_amount``, ``format_datetime``).
    """
    trzba = etree.Element(_tag(NS_EET, "Trzba"), nsmap={"v4": NS_EET})
    hlavicka = etree.SubElement(trzba, _tag(NS_EET, "Hlavicka"))
    hlavicka.set("uuid_zpravy", values["uuid_zpravy"])
    hlavicka.set("dat_odesl", values["dat_odesl"])
    hlavicka.set("prvni_zaslani", _bool(values["prvni_zaslani"]))
    if values.get("overeni"):
        hlavicka.set("overeni", "true")

    data = etree.SubElement(trzba, _tag(NS_EET, "Data"))
    data.set("eic_popl", values["eic_popl"])
    if values.get("eic_poverujiciho"):
        data.set("eic_poverujiciho", values["eic_poverujiciho"])
    if values.get("povereni_vice_popl"):
        data.set("povereni_vice_popl", "true")
    data.set("id_jednotky", str(int(values["id_jednotky"])))
    data.set("id_pokl", values["id_pokl"])
    data.set("porad_cis", values["porad_cis"])
    data.set("dat_trzby", values["dat_trzby"])
    data.set("celk_trzba", values["celk_trzba"])
    for key in ("urceno_cerp_zuct", "cerp_zuct"):
        if values.get(key) not in (None, False, ""):
            data.set(key, values[key])
    return trzba


def _c14n(element: etree._Element, prefixes: list[str] | None) -> bytes:
    return etree.tostring(
        element,
        method="c14n",
        exclusive=True,
        with_comments=False,
        inclusive_ns_prefixes=prefixes or None,
    )


def _digest(element: etree._Element, prefixes: list[str] | None) -> str:
    return _b64(hashlib.sha256(_c14n(element, prefixes)).digest())


def build_signed_envelope(trzba: etree._Element, certificate_der: bytes, sign) -> bytes:
    """Podepsaná SOAP obálka podle WS-Security.

    :param trzba: element ``<v4:Trzba>`` (``build_trzba``)
    :param certificate_der: pokladní certifikát v DER
    :param sign: funkce ``bytes -> bytes`` vracející surový RSA-SHA256 (PKCS#1 v1.5) podpis
    :return: serializovaná obálka v UTF-8
    """
    body_id = "id-" + uuid.uuid4().hex.upper()
    token_id = "X509-" + uuid.uuid4().hex.upper()

    envelope = etree.Element(_tag(NS_SOAP, "Envelope"), nsmap={"soapenv": NS_SOAP, "v4": NS_EET})
    header = etree.SubElement(envelope, _tag(NS_SOAP, "Header"))
    security = etree.SubElement(header, _tag(NS_WSSE, "Security"), nsmap={"wsse": NS_WSSE, "wsu": NS_WSU})
    token = etree.SubElement(security, _tag(NS_WSSE, "BinarySecurityToken"))
    token.set("EncodingType", BST_ENCODING)
    token.set("ValueType", BST_VALUE_TYPE)
    token.set(_tag(NS_WSU, "Id"), token_id)
    token.text = _b64(certificate_der)

    body = etree.SubElement(envelope, _tag(NS_SOAP, "Body"), nsmap={"wsu": NS_WSU})
    body.set(_tag(NS_WSU, "Id"), body_id)
    body.append(trzba)
    # Stejné rozložení deklarací jako ve vzorech FS: soapenv a v4 na obálce, wsu na Body,
    # Trzba bez vlastní (duplicitní) deklarace. Prefixy z InclusiveNamespaces jsou tak
    # v dosahu všude, kde se na ně podpis odkazuje.
    etree.cleanup_namespaces(
        envelope, top_nsmap={"soapenv": NS_SOAP, "v4": NS_EET}, keep_ns_prefixes=["soapenv", "v4"]
    )

    signature = etree.SubElement(security, _tag(NS_DS, "Signature"), nsmap={"ds": NS_DS})
    signature.set("Id", "SIG-" + uuid.uuid4().hex.upper())
    signed_info = etree.SubElement(signature, _tag(NS_DS, "SignedInfo"))
    c14n_method = etree.SubElement(signed_info, _tag(NS_DS, "CanonicalizationMethod"))
    c14n_method.set("Algorithm", ALG_EXC_C14N)
    inclusive = etree.SubElement(c14n_method, _tag(NS_EC, "InclusiveNamespaces"), nsmap={"ec": NS_EC})
    inclusive.set("PrefixList", "soapenv v4")
    etree.SubElement(signed_info, _tag(NS_DS, "SignatureMethod")).set("Algorithm", ALG_RSA_SHA256)
    reference = etree.SubElement(signed_info, _tag(NS_DS, "Reference"))
    reference.set("URI", "#" + body_id)
    transforms = etree.SubElement(reference, _tag(NS_DS, "Transforms"))
    transform = etree.SubElement(transforms, _tag(NS_DS, "Transform"))
    transform.set("Algorithm", ALG_EXC_C14N)
    inclusive = etree.SubElement(transform, _tag(NS_EC, "InclusiveNamespaces"), nsmap={"ec": NS_EC})
    inclusive.set("PrefixList", "v4")
    etree.SubElement(reference, _tag(NS_DS, "DigestMethod")).set("Algorithm", ALG_SHA256)
    etree.SubElement(reference, _tag(NS_DS, "DigestValue"))

    etree.SubElement(signature, _tag(NS_DS, "SignatureValue"))
    key_info = etree.SubElement(signature, _tag(NS_DS, "KeyInfo"))
    key_info.set("Id", "KI-" + uuid.uuid4().hex.upper())
    token_reference = etree.SubElement(key_info, _tag(NS_WSSE, "SecurityTokenReference"))
    token_reference.set(_tag(NS_WSU, "Id"), "STR-" + uuid.uuid4().hex.upper())
    reference_element = etree.SubElement(token_reference, _tag(NS_WSSE, "Reference"))
    reference_element.set("URI", "#" + token_id)
    reference_element.set("ValueType", BST_VALUE_TYPE)

    # Otisk a podpis se počítají až nad znovu naparsovaným dokumentem. Nad stromem
    # sestaveným v paměti libxml2 občas (nedeterministicky, řádově 1 z 10–200 zpráv)
    # nevidí deklarace jmenných prostorů předků pro InclusiveNamespaces, takže otisk
    # i podpis neodpovídají odeslanému XML a server vrátí „Neplatný podpis“ (kód 4).
    # Naparsovaný strom je přesně to, co uvidí server; výsledek se navíc sám ověří.
    serialized = etree.tostring(envelope, xml_declaration=True, encoding="UTF-8")
    parser = etree.XMLParser(resolve_entities=False, remove_blank_text=False)
    for _attempt in range(3):
        root = etree.fromstring(serialized, parser)
        body = root.find(_tag(NS_SOAP, "Body"))
        signed_info = root.find(f".//{_tag(NS_DS, 'SignedInfo')}")
        signed_info.find(f".//{_tag(NS_DS, 'DigestValue')}").text = _digest(body, ["v4"])
        root.find(f".//{_tag(NS_DS, 'SignatureValue')}").text = _b64(sign(_c14n(signed_info, ["soapenv", "v4"])))
        result = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
        check = verify_envelope(result)
        if check["digest_ok"] and check["signature_ok"]:
            return result
    raise ValueError("Podpis SOAP zprávy nesouhlasí s vlastní kontrolou (kanonizace XML).")


def _prefix_list(element: etree._Element | None) -> list[str]:
    if element is None:
        return []
    return (element.get("PrefixList") or "").split()


def verify_envelope(envelope: bytes) -> dict:
    """Kontrola otisku Body a RSA podpisu certifikátem z ``BinarySecurityToken``.

    Vrací ``{"digest_ok": bool, "signature_ok": bool, "certificate": x509.Certificate}``.
    Slouží pro testy a pro ověření kompatibility se vzorovými zprávami FS.
    """
    root = etree.fromstring(envelope)
    body = root.find(_tag(NS_SOAP, "Body"))
    security = root.find(f".//{_tag(NS_WSSE, 'Security')}")
    signature = security.find(_tag(NS_DS, "Signature"))
    signed_info = signature.find(_tag(NS_DS, "SignedInfo"))
    reference = signed_info.find(_tag(NS_DS, "Reference"))

    body_prefixes = _prefix_list(reference.find(f".//{_tag(NS_EC, 'InclusiveNamespaces')}"))
    expected_digest = (reference.findtext(_tag(NS_DS, "DigestValue")) or "").strip()
    digest_ok = _digest(body, body_prefixes) == expected_digest

    signed_info_prefixes = _prefix_list(
        signed_info.find(f"{_tag(NS_DS, 'CanonicalizationMethod')}/{_tag(NS_EC, 'InclusiveNamespaces')}")
    )
    certificate = x509.load_der_x509_certificate(
        base64.b64decode(security.findtext(_tag(NS_WSSE, "BinarySecurityToken")))
    )
    signature_bytes = base64.b64decode(signature.findtext(_tag(NS_DS, "SignatureValue")))
    try:
        certificate.public_key().verify(
            signature_bytes, _c14n(signed_info, signed_info_prefixes), padding.PKCS1v15(), hashes.SHA256()
        )
        signature_ok = True
    except InvalidSignature:
        signature_ok = False
    return {"digest_ok": digest_ok, "signature_ok": signature_ok, "certificate": certificate}


def post_envelope(url: str, payload: bytes, timeout: float = DEFAULT_TIMEOUT) -> requests.Response:
    """HTTP POST podepsané obálky (SOAP 1.1). Výjimky ``requests`` nechává projít."""
    return requests.post(
        url,
        data=payload,
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
        timeout=timeout,
    )


def _text_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1")


def parse_response(content: bytes) -> dict:
    """Rozbor potvrzovací nebo chybové datové zprávy (případně SOAP Fault).

    Vrací slovník s klíči ``fault`` (text SOAP Fault nebo ``None``), ``uuid_zpravy``,
    ``dat_prij``, ``dat_odmit`` (naivní UTC), ``pok``, ``test`` (příznak neprodukčního
    prostředí), ``kod`` (kód chyby jako řetězec, ``"0"`` = ověřovací mód OK, nebo ``None``),
    ``chyba`` (text chyby) a ``varovani`` (seznam dvojic ``(kod_varov, text)``).
    """
    root = etree.fromstring(content)
    result = {
        "fault": None,
        "uuid_zpravy": None,
        "dat_prij": None,
        "dat_odmit": None,
        "pok": None,
        "test": False,
        "kod": None,
        "chyba": None,
        "varovani": [],
    }
    fault = root.find(f".//{_tag(NS_SOAP, 'Fault')}")
    if fault is not None:
        result["fault"] = " ".join((fault.findtext("faultstring") or "SOAP Fault").split())
        return result

    odpoved = root.find(f".//{_tag(NS_EET, 'Odpoved')}")
    if odpoved is None:
        raise ValueError("Odpověď neobsahuje element Odpoved.")

    hlavicka = odpoved.find(_tag(NS_EET, "Hlavicka"))
    if hlavicka is not None:
        result["uuid_zpravy"] = hlavicka.get("uuid_zpravy")
        result["dat_prij"] = parse_datetime(hlavicka.get("dat_prij"))
        result["dat_odmit"] = parse_datetime(hlavicka.get("dat_odmit"))

    potvrzeni = odpoved.find(_tag(NS_EET, "Potvrzeni"))
    if potvrzeni is not None:
        result["pok"] = (potvrzeni.get("pok") or "").strip() or None
        result["test"] = _text_bool(potvrzeni.get("test"))

    chyba = odpoved.find(_tag(NS_EET, "Chyba"))
    if chyba is not None:
        result["kod"] = (chyba.get("kod") or "").strip()
        result["chyba"] = " ".join((chyba.text or "").split())
        result["test"] = _text_bool(chyba.get("test"))

    for varovani in odpoved.findall(_tag(NS_EET, "Varovani")):
        result["varovani"].append((varovani.get("kod_varov"), " ".join((varovani.text or "").split())))
    return result
