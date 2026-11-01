# -*- coding: utf-8 -*-
from datetime import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12
from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.cz_eet.models import eet_soap

MODULE_DIR = Path(eet_soap.__file__).resolve().parents[1]
SAMPLE_REQUEST = MODULE_DIR / "tests" / "files" / "CZ8551015704.eet.v4.req.xml"
PLAYGROUND_P12 = MODULE_DIR / "static" / "playground" / "CA_EET-Playground-CZ8551015704.p12"
PLAYGROUND_PASSWORD = b"aaaa1111"

RESPONSE_POTVRZENI = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Header/><soapenv:Body>
<eet:Odpoved xmlns:eet="http://fs.gov.cz/eet/schema/v4">
  <eet:Hlavicka uuid_zpravy="123e4567-e89b-42d3-a456-426655440000" dat_prij="2027-03-04T18:25:21+01:00"/>
  <eet:Potvrzeni pok="987a6be5-6af5-44f3-b4fc-987654321000-ff" test="true"/>
  <eet:Varovani kod_varov="1">EIC poplatnika v datove zprave se neshoduje s EIC v certifikatu</eet:Varovani>
</eet:Odpoved></soapenv:Body></soapenv:Envelope>"""

RESPONSE_CHYBA = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>
<eet:Odpoved xmlns:eet="http://fs.gov.cz/eet/schema/v4">
  <eet:Hlavicka dat_odmit="2027-03-04T18:25:21+01:00"/>
  <eet:Chyba kod="-1">Docasna technicka chyba zpracovani - odeslete prosim
   datovou zpravu pozdeji</eet:Chyba>
</eet:Odpoved></soapenv:Body></soapenv:Envelope>"""

RESPONSE_OVERENI = b"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>
<eet:Odpoved xmlns:eet="http://fs.gov.cz/eet/schema/v4">
  <eet:Hlavicka uuid_zpravy="123e4567-e89b-42d3-a456-426655440000" dat_odmit="2027-03-04T18:25:21+01:00"/>
  <eet:Chyba kod="0" test="true">Datovou zpravu evidovane trzby v overovacim modu se podarilo zpracovat</eet:Chyba>
  <eet:Varovani kod_varov="5">id_jednotky neodpovida formatem pridelenemu c. evidencni jednotky</eet:Varovani>
</eet:Odpoved></soapenv:Body></soapenv:Envelope>"""

RESPONSE_FAULT = b"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"><env:Body><env:Fault>
<faultcode>env:Client</faultcode><faultstring>EET Request processing: Unable to parse</faultstring>
</env:Fault></env:Body></env:Envelope>"""


def sample_values(**overrides):
    values = {
        "uuid_zpravy": "b3a09b52-7c87-4014-a496-4c7a53cf9125",
        "dat_odesl": "2027-01-09T04:25:28+01:00",
        "prvni_zaslani": True,
        "overeni": False,
        "eic_popl": "CZ8551015704",
        "id_jednotky": 21,
        "id_pokl": "OBCHUDEK-1",
        "porad_cis": "261-1-000005",
        "dat_trzby": "2027-01-09T04:25:10+01:00",
        "celk_trzba": "250.00",
    }
    values.update(overrides)
    return values


def load_playground_key():
    key, certificate, _chain = pkcs12.load_key_and_certificates(PLAYGROUND_P12.read_bytes(), PLAYGROUND_PASSWORD)
    return key, certificate


@tagged("post_install", "-at_install")
class TestEetSoap(TransactionCase):
    def test_format_amount(self):
        self.assertEqual(eet_soap.format_amount(250), "250.00")
        self.assertEqual(eet_soap.format_amount(-187.2), "-187.20")
        self.assertEqual(eet_soap.format_amount(0.5), "0.50")
        self.assertEqual(eet_soap.format_amount(-0.5), "-0.50")
        self.assertEqual(eet_soap.format_amount(0), "0.00")
        self.assertEqual(eet_soap.format_amount(-0.0), "0.00")
        self.assertEqual(eet_soap.format_amount(-0.001), "0.00")
        self.assertEqual(eet_soap.format_amount(99999999.99), "99999999.99")
        with self.assertRaises(ValueError):
            eet_soap.format_amount(100_000_000)

    def test_format_datetime_czech_offsets(self):
        """Odoo ukládá UTC; zpráva chce místní čas se zónou SEČ/SELČ."""
        self.assertEqual(eet_soap.format_datetime(datetime(2027, 1, 9, 3, 25, 28)), "2027-01-09T04:25:28+01:00")
        self.assertEqual(eet_soap.format_datetime(datetime(2027, 6, 9, 3, 25, 28, 999)), "2027-06-09T05:25:28+02:00")

    def test_parse_datetime(self):
        self.assertEqual(eet_soap.parse_datetime("2027-03-04T18:25:21+01:00"), datetime(2027, 3, 4, 17, 25, 21))
        self.assertEqual(eet_soap.parse_datetime("2026-07-01T09:01:15Z"), datetime(2026, 7, 1, 9, 1, 15))
        self.assertIsNone(eet_soap.parse_datetime(None))

    def test_sanitize_identifier(self):
        self.assertEqual(eet_soap.sanitize_identifier("Obchůdek", 20), "Obchudek")
        self.assertEqual(eet_soap.sanitize_identifier("Zmrzlina – okénko #1", 20), "Zmrzlina okenko #1")
        self.assertEqual(eet_soap.sanitize_identifier("Order 00012-001-0003", 25), "Order 00012-001-0003")
        self.assertEqual(eet_soap.sanitize_identifier("x" * 30, 20), "x" * 20)
        self.assertEqual(eet_soap.sanitize_identifier(None, 20), "")
        self.assertTrue(eet_soap.RE_ID_POKL.match(eet_soap.sanitize_identifier("Trhy – Kaplice 2027", 20)))

    def test_build_trzba_valid_against_xsd(self):
        trzba = eet_soap.build_trzba(sample_values())
        self.assertEqual(eet_soap.xsd_errors(trzba), [])
        hlavicka = trzba.find(f"{{{eet_soap.NS_EET}}}Hlavicka")
        data = trzba.find(f"{{{eet_soap.NS_EET}}}Data")
        self.assertIsNone(hlavicka.get("overeni"), "ostrý mód: atribut overeni se vynechává")
        self.assertEqual(hlavicka.get("prvni_zaslani"), "true")
        self.assertEqual(data.get("celk_trzba"), "250.00")
        self.assertEqual(data.get("id_jednotky"), "21")
        for optional in ("eic_poverujiciho", "povereni_vice_popl", "urceno_cerp_zuct", "cerp_zuct"):
            self.assertIsNone(data.get(optional))

    def test_build_trzba_overeni_and_repeat(self):
        trzba = eet_soap.build_trzba(sample_values(overeni=True, prvni_zaslani=False, celk_trzba="-187.20"))
        self.assertEqual(eet_soap.xsd_errors(trzba), [])
        hlavicka = trzba.find(f"{{{eet_soap.NS_EET}}}Hlavicka")
        self.assertEqual(hlavicka.get("overeni"), "true")
        self.assertEqual(hlavicka.get("prvni_zaslani"), "false")

    def test_build_trzba_vouchers(self):
        """Hodnotový poukaz: nabití a čerpání jsou nepovinné atributy, obojí je i v celk_trzba."""
        trzba = eet_soap.build_trzba(sample_values(celk_trzba="1050.00", urceno_cerp_zuct="1000.00"))
        self.assertEqual(eet_soap.xsd_errors(trzba), [])
        data = trzba.find(f"{{{eet_soap.NS_EET}}}Data")
        self.assertEqual(data.get("urceno_cerp_zuct"), "1000.00")
        self.assertIsNone(data.get("cerp_zuct"))
        trzba = eet_soap.build_trzba(sample_values(celk_trzba="-121.00", cerp_zuct="-121.00"))
        self.assertEqual(eet_soap.xsd_errors(trzba), [])

    def test_certificate_environment(self):
        """Vydavatele pokladního certifikátu pozná modul podle certifikátů CA EET z eet.gov.cz."""
        self.assertEqual(set(eet_soap._issuing_cas()), {"production", "playground"}, "SubCA musí navazovat na kořenovou CA")
        _key, certificate = load_playground_key()
        self.assertEqual(eet_soap.certificate_environment(certificate.public_bytes(Encoding.DER)), "playground")
        # kořenová CA je podepsaná sama sebou, pokladní certifikát to není
        root = x509.load_pem_x509_certificate((eet_soap.CA_DIR / "ca_eet-root_prod.pem").read_bytes())
        self.assertIsNone(eet_soap.certificate_environment(root.public_bytes(Encoding.DER)))
        self.assertIsNone(eet_soap.certificate_environment(b"neni certifikat"))

    def test_build_trzba_invalid_values_fail_xsd(self):
        self.assertTrue(eet_soap.xsd_errors(eet_soap.build_trzba(sample_values(eic_popl="12345678"))))
        self.assertTrue(eet_soap.xsd_errors(eet_soap.build_trzba(sample_values(celk_trzba="250"))))
        self.assertTrue(eet_soap.xsd_errors(eet_soap.build_trzba(sample_values(porad_cis="číslo s diakritikou"))))
        self.assertTrue(eet_soap.xsd_errors(eet_soap.build_trzba(sample_values(dat_trzby="2027-01-09 04:25:10"))))

    def test_official_sample_verifies(self):
        """Vzorová zpráva FS: náš výpočet otisku i podpisu musí souhlasit s jejím obsahem."""
        result = eet_soap.verify_envelope(SAMPLE_REQUEST.read_bytes())
        self.assertTrue(result["digest_ok"], "Exclusive C14N otisk Body nesouhlasí se vzorem FS")
        self.assertTrue(result["signature_ok"], "podpis SignedInfo nesouhlasí se vzorem FS")
        self.assertEqual(result["certificate"].subject.rfc4514_string().split(",")[1], "CN=CZ8551015704")

    def test_sign_and_verify_own_envelope(self):
        key, certificate = load_playground_key()

        def sign(data):
            return key.sign(data, padding.PKCS1v15(), hashes.SHA256())

        envelope = eet_soap.build_signed_envelope(
            eet_soap.build_trzba(sample_values()), certificate.public_bytes(Encoding.DER), sign
        )
        self.assertLess(len(envelope), eet_soap.MAX_MESSAGE_BYTES)
        self.assertTrue(envelope.startswith(b"<?xml version='1.0' encoding='UTF-8'?>"))
        result = eet_soap.verify_envelope(envelope)
        self.assertTrue(result["digest_ok"])
        self.assertTrue(result["signature_ok"])
        self.assertEqual(result["certificate"].serial_number, certificate.serial_number)

        root = etree.fromstring(envelope)
        body = root.find(f"{{{eet_soap.NS_SOAP}}}Body")
        self.assertEqual(len(body), 1, "Body obsahuje právě element Trzba")
        self.assertEqual(eet_soap.xsd_errors(body[0]), [])
        self.assertIsNotNone(body.get(f"{{{eet_soap.NS_WSU}}}Id"))
        header = root.find(f"{{{eet_soap.NS_SOAP}}}Header")
        self.assertEqual(len(header), 1, "hlavička má jen wsse:Security (žádný Timestamp ani WS-Addressing)")
        signed_info = root.find(f".//{{{eet_soap.NS_DS}}}SignedInfo")
        self.assertEqual(
            signed_info.find(f"{{{eet_soap.NS_DS}}}SignatureMethod").get("Algorithm"), eet_soap.ALG_RSA_SHA256
        )
        self.assertEqual(signed_info.find(f".//{{{eet_soap.NS_DS}}}DigestMethod").get("Algorithm"), eet_soap.ALG_SHA256)
        self.assertEqual(signed_info.find(f".//{{{eet_soap.NS_DS}}}Reference").get("URI"), "#" + body.get(f"{{{eet_soap.NS_WSU}}}Id"))

    def test_many_envelopes_stay_consistent(self):
        """Regrese: kanonizace nad stromem v paměti občas nevidí deklarace předků (kód 4 od FS).

        Otisk i podpis se proto počítají nad znovu naparsovaným dokumentem; 100 obálek
        za sebou musí projít lokální kontrolou a Body musí mít xmlns:v4 z obálky, ne z Trzby.
        """
        key, certificate = load_playground_key()
        der = certificate.public_bytes(Encoding.DER)
        for _ in range(100):
            envelope = eet_soap.build_signed_envelope(
                eet_soap.build_trzba(sample_values(uuid_zpravy=eet_soap.new_uuid())),
                der,
                lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
            )
            result = eet_soap.verify_envelope(envelope)
            self.assertTrue(result["digest_ok"] and result["signature_ok"])
            self.assertNotIn(b"<v4:Trzba xmlns", envelope)
            self.assertIn(b'xmlns:v4="http://fs.gov.cz/eet/schema/v4"', envelope[:200])

    def test_tampered_envelope_fails(self):
        key, certificate = load_playground_key()
        envelope = eet_soap.build_signed_envelope(
            eet_soap.build_trzba(sample_values()),
            certificate.public_bytes(Encoding.DER),
            lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
        )
        tampered = envelope.replace(b'celk_trzba="250.00"', b'celk_trzba="25.00"')
        self.assertFalse(eet_soap.verify_envelope(tampered)["digest_ok"])

    def test_parse_response_potvrzeni(self):
        parsed = eet_soap.parse_response(RESPONSE_POTVRZENI)
        self.assertIsNone(parsed["fault"])
        self.assertEqual(parsed["pok"], "987a6be5-6af5-44f3-b4fc-987654321000-ff")
        self.assertTrue(parsed["test"])
        self.assertEqual(parsed["dat_prij"], datetime(2027, 3, 4, 17, 25, 21))
        self.assertIsNone(parsed["kod"])
        self.assertEqual(parsed["varovani"], [("1", "EIC poplatnika v datove zprave se neshoduje s EIC v certifikatu")])
        self.assertTrue(eet_soap.RE_POK.match(parsed["pok"]))

    def test_parse_response_chyba(self):
        parsed = eet_soap.parse_response(RESPONSE_CHYBA)
        self.assertIsNone(parsed["pok"])
        self.assertEqual(parsed["kod"], "-1")
        self.assertEqual(parsed["chyba"], "Docasna technicka chyba zpracovani - odeslete prosim datovou zpravu pozdeji")
        self.assertEqual(parsed["dat_odmit"], datetime(2027, 3, 4, 17, 25, 21))
        self.assertFalse(parsed["test"])

    def test_parse_response_overeni(self):
        parsed = eet_soap.parse_response(RESPONSE_OVERENI)
        self.assertEqual(parsed["kod"], "0")
        self.assertTrue(parsed["test"])
        self.assertEqual(parsed["varovani"][0][0], "5")

    def test_parse_response_fault(self):
        parsed = eet_soap.parse_response(RESPONSE_FAULT)
        self.assertEqual(parsed["fault"], "EET Request processing: Unable to parse")
        self.assertIsNone(parsed["pok"])

    def test_parse_response_without_odpoved(self):
        with self.assertRaises(ValueError):
            eet_soap.parse_response(b"<html><body>Bad gateway</body></html>")
