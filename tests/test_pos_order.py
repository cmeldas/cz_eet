# -*- coding: utf-8 -*-
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.cz_eet.models import eet_soap
from odoo.addons.point_of_sale.tests.common import TestPoSCommon

PLAYGROUND_P12 = Path(eet_soap.__file__).resolve().parents[1] / "static" / "playground" / "CA_EET-Playground-CZ8551015704.p12"
POK = "987a6be5-6af5-44f3-b4fc-987654321000-ff"
POST_TARGET = "odoo.addons.cz_eet.models.eet_soap.post_envelope"


def odpoved(inner):
    return (
        b'<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        b'<soapenv:Body><eet:Odpoved xmlns:eet="http://fs.gov.cz/eet/schema/v4">' + inner + b"</eet:Odpoved></soapenv:Body></soapenv:Envelope>"
    )


RESPONSE_POK = odpoved(
    b'<eet:Hlavicka uuid_zpravy="x" dat_prij="2026-09-13T10:00:00+02:00"/>'
    b'<eet:Potvrzeni pok="' + POK.encode() + b'" test="true"/>'
)
RESPONSE_TEMPORARY = odpoved(
    b'<eet:Hlavicka dat_odmit="2026-09-13T10:00:00+02:00"/>'
    b'<eet:Chyba kod="-1" test="true">Docasna technicka chyba zpracovani - odeslete prosim datovou zpravu pozdeji</eet:Chyba>'
)
RESPONSE_SIGNATURE_ERROR = odpoved(
    b'<eet:Hlavicka dat_odmit="2026-09-13T10:00:00+02:00"/><eet:Chyba kod="4" test="true">Neplatny podpis SOAP zpravy</eet:Chyba>'
)
RESPONSE_OVERENI_OK = odpoved(
    b'<eet:Hlavicka uuid_zpravy="x" dat_odmit="2026-09-13T10:00:00+02:00"/>'
    b'<eet:Chyba kod="0" test="true">Datovou zpravu evidovane trzby v overovacim modu se podarilo zpracovat</eet:Chyba>'
)


def fake_response(xml, status_code=200):
    return SimpleNamespace(
        status_code=status_code, content=xml, text=xml.decode("utf-8"), headers={"X-Global-Transaction-Id": "tx-1"}
    )


class FakeServer:
    """Náhrada ``post_envelope``: vrací připravené odpovědi a pamatuje si odeslané obálky."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, payload, timeout):
        self.requests.append((url, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return fake_response(response)


@tagged("post_install", "-at_install")
class TestEetPosOrder(TestPoSCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.certificate = cls.env["certificate.certificate"].create({
            "name": "EET Playground test",
            "scope": "eet",
            "company_id": cls.company.id,
            "content": base64.b64encode(PLAYGROUND_P12.read_bytes()),
            "pkcs12_password": "aaaa1111",
        })
        cls.config = cls.basic_config
        cls.config.write({
            "eet_enabled": True,
            "eet_environment": "playground",
            "eet_certificate_id": cls.certificate.id,
            "eet_id_jednotky": 21,
            "eet_id_pokl": "TEST-1",
        })
        cls.product = cls.create_product("Zmrzlina kopeček", cls.categ_basic, 45.0)
        cls.free_product = cls.create_product("Ochutnávka", cls.categ_basic, 0.0)

    def setUp(self):
        super().setUp()
        self.open_new_session()

    def _create_paid_order(self, lines=None, payments=None):
        orders = self._create_orders([{
            "pos_order_lines_ui_args": lines or [(self.product, 2)],
            "payments": payments,
        }])
        return next(iter(orders.values()))

    # ------------------------------------------------------------------
    # Nastavení pokladny
    # ------------------------------------------------------------------

    def test_eic_prefilled_from_certificate(self):
        self.assertEqual(self.config.eet_eic, "CZ8551015704")
        self.assertEqual(self.config.eet_url, eet_soap.URL_PLAYGROUND)

    def test_id_pokl_default_from_name(self):
        config = self.env["pos.config"].create({"name": "Obchůdek – okénko"})
        self.assertEqual(config.eet_id_pokl, "Obchudek okenko")

    def test_constraints(self):
        with self.assertRaises(ValidationError):
            self.config.write({"eet_eic": "CZ1"})
        with self.assertRaises(ValidationError):
            self.config.write({"eet_id_jednotky": 0})
        with self.assertRaises(ValidationError):
            self.config.write({"eet_id_pokl": "pokladna č. 1"})
        with self.assertRaises(ValidationError):
            self.config.write({"eet_certificate_id": False})

    # ------------------------------------------------------------------
    # Tok objednávky
    # ------------------------------------------------------------------

    def test_paid_order_creates_trzba_and_sends(self):
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            trzba = order.eet_trzba_ids
            self.assertEqual(len(trzba), 1)
            self.assertEqual(trzba.state, "to_send", "po synchronizaci tržba čeká, odeslání si vyžádá pokladna")
            self.assertEqual(server.requests, [])

            result = self.env["pos.order"].eet_send_now([order.id])

        self.assertEqual(result, {order.id: {"eet_state": "sent", "eet_pok": POK}})
        self.assertEqual(trzba.state, "sent")
        self.assertEqual(trzba.pok, POK)
        self.assertTrue(trzba.test)
        self.assertEqual(trzba.attempts, 1)
        self.assertEqual(trzba.transaction_id, "tx-1")
        self.assertEqual(trzba.environment, "playground")
        self.assertEqual(trzba.porad_cis, order.pos_reference)
        self.assertEqual(trzba.celk_trzba, 90.0)
        self.assertEqual(trzba.dat_trzby, order.date_order)
        self.assertEqual(order.eet_pok, POK)
        self.assertEqual(order.eet_state, "sent")

        url, payload = server.requests[0]
        self.assertEqual(url, eet_soap.URL_PLAYGROUND)
        self.assertIn(b'eic_popl="CZ8551015704"', payload)
        self.assertIn(b'id_jednotky="21"', payload)
        self.assertIn(b'id_pokl="TEST-1"', payload)
        self.assertIn(f'porad_cis="{order.pos_reference}"'.encode(), payload)
        self.assertIn(b'celk_trzba="90.00"', payload)
        self.assertIn(b'prvni_zaslani="true"', payload)
        self.assertNotIn(b"overeni", payload)
        self.assertTrue(eet_soap.verify_envelope(payload)["signature_ok"])
        self.assertEqual(trzba.request_xml, payload.decode("utf-8"))
        self.assertIn(POK, trzba.response_xml)

    def test_refund_is_negative_amount(self):
        cash = self.config.payment_method_ids.filtered("is_cash_count")[:1]
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order(lines=[(self.product, -1)], payments=[(cash, -45.0)])
            self.env["pos.order"].eet_send_now([order.id])
        self.assertEqual(order.eet_trzba_ids.celk_trzba, -45.0)
        self.assertIn(b'celk_trzba="-45.00"', server.requests[0][1])

    def test_temporary_error_is_retried_as_repeat(self):
        server = FakeServer(RESPONSE_TEMPORARY, RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            result = self.env["pos.order"].eet_send_now([order.id])
            trzba = order.eet_trzba_ids
            self.assertEqual(result[order.id], {"eet_state": "to_send", "eet_pok": False})
            self.assertEqual(trzba.state, "to_send")
            self.assertEqual(trzba.attempts, 1)
            self.assertEqual(trzba.response_code, "-1")
            self.assertTrue(trzba.error_text.startswith("-1:"))
            first_uuid = trzba.uuid_zpravy

            self.env["eet.trzba"]._cron_send_pending()

        self.assertEqual(trzba.state, "sent")
        self.assertEqual(trzba.attempts, 2)
        self.assertNotEqual(trzba.uuid_zpravy, first_uuid, "opakované zaslání má nové UUID zprávy")
        self.assertIn(b'prvni_zaslani="false"', server.requests[1][1])
        self.assertFalse(trzba.error_text)

    def test_critical_error_needs_manual_retry(self):
        server = FakeServer(RESPONSE_SIGNATURE_ERROR, RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            self.env["pos.order"].eet_send_now([order.id])
            trzba = order.eet_trzba_ids
            self.assertEqual(trzba.state, "error")
            self.assertEqual(trzba.response_code, "4")

            self.env["eet.trzba"]._cron_send_pending()
            self.assertEqual(trzba.state, "error", "kritickou chybu cron neopakuje")
            self.assertEqual(len(server.requests), 1)

            order.action_eet_send()
        self.assertEqual(trzba.state, "sent")
        self.assertEqual(len(server.requests), 2)

    def test_network_error_keeps_waiting(self):
        server = FakeServer(requests.ConnectionError("Name or service not known"))
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            result = self.env["pos.order"].eet_send_now([order.id])
        trzba = order.eet_trzba_ids
        self.assertEqual(trzba.state, "to_send")
        self.assertEqual(trzba.attempts, 1)
        self.assertIn("Spojení se nezdařilo", trzba.error_text)
        self.assertFalse(trzba.response_xml)
        self.assertEqual(result[order.id]["eet_pok"], False)

    def test_overeni_mode(self):
        self.config.eet_overeni = True
        server = FakeServer(RESPONSE_OVERENI_OK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            result = self.env["pos.order"].eet_send_now([order.id])
        trzba = order.eet_trzba_ids
        self.assertTrue(trzba.overeni)
        self.assertEqual(trzba.state, "verified")
        self.assertEqual(trzba.response_code, "0")
        self.assertFalse(trzba.pok)
        self.assertEqual(result[order.id], {"eet_state": "verified", "eet_pok": False})
        self.assertIn(b'overeni="true"', server.requests[0][1])

    def test_zero_amount_has_no_trzba(self):
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order(lines=[(self.free_product, 1)], payments=[])
        self.assertFalse(order.eet_trzba_ids)
        self.assertFalse(order.eet_state)

    def test_disabled_config_has_no_trzba(self):
        self.config.eet_enabled = False
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order()
        self.assertFalse(order.eet_trzba_ids)
        self.assertEqual(self.env["pos.order"].eet_send_now([order.id]), {order.id: {"eet_state": False, "eet_pok": False}})

    def test_settings_snapshot_survives_config_change(self):
        """Tržba si drží prostředí a EIČ z okamžiku vzniku i po přepnutí pokladny."""
        with patch(POST_TARGET, FakeServer(RESPONSE_POK)):
            order = self._create_paid_order()
            self.config.eet_environment = "production"
            self.assertEqual(order.eet_trzba_ids._get_url(), eet_soap.URL_PLAYGROUND)

    # ------------------------------------------------------------------
    # Test spojení z nastavení
    # ------------------------------------------------------------------

    def test_connection_test_action(self):
        server = FakeServer(RESPONSE_OVERENI_OK)
        with patch(POST_TARGET, server):
            action = self.config.action_eet_test_connection()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")
        trzba = self.env["eet.trzba"].search([("config_id", "=", self.config.id), ("pos_order_id", "=", False)])
        self.assertEqual(len(trzba), 1)
        self.assertTrue(trzba.overeni)
        self.assertEqual(trzba.state, "verified")
        self.assertEqual(trzba.celk_trzba, 0.0)
        self.assertTrue(trzba.porad_cis.startswith("TEST-"))
        self.assertIn(b'overeni="true"', server.requests[0][1])
        self.assertIn(b'celk_trzba="0.00"', server.requests[0][1])

    def test_connection_test_failure(self):
        with patch(POST_TARGET, FakeServer(RESPONSE_SIGNATURE_ERROR)):
            action = self.config.action_eet_test_connection()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("Neplatny podpis", action["params"]["message"])
