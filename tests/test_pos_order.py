# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import BinaryBytes

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
            "content": BinaryBytes(PLAYGROUND_P12.read_bytes()),
            "pkcs12_password": "aaaa1111",
        })
        # Hotovost se zaokrouhluje na celé koruny, karta ne (jako v obchůdku na statku).
        rounding = cls.env["account.cash.rounding"].create({
            "name": "Celé koruny",
            "rounding": 1.0,
            "rounding_method": "HALF-UP",
            "profit_account_id": cls.company_data["default_account_revenue"].id,
            "loss_account_id": cls.company_data["default_account_expense"].id,
        })
        cls.config = cls.basic_config
        cls.config.write({
            "eet_enabled": True,
            "eet_environment": "playground",
            "eet_certificate_id": cls.certificate.id,
            "eet_id_jednotky": 21,
            "eet_id_pokl": "TEST-1",
            "cash_rounding": True,
            "rounding_method": rounding.id,
            "only_round_cash_method": True,
        })
        cls.product = cls.create_product("Zmrzlina kopeček", cls.categ_basic, 45.0)
        cls.free_product = cls.create_product("Ochutnávka", cls.categ_basic, 0.0)
        cls.fruit = cls.create_product("Ovoce", cls.categ_basic, 40.80)

    def setUp(self):
        super().setUp()
        self.open_new_session()
        self.cash = self.config.payment_method_ids.filtered(lambda method: method.type == "cash")[:1]
        self.card = self.config.payment_method_ids.filtered(lambda method: method.type == "bank")[:1]

    def _create_paid_order(self, lines=None, payments=None, **order_values):
        orders = self._create_orders([{
            "pos_order_lines_ui_args": lines or [(self.product, 2)],
            "payments": payments,
            "pos_order_ui_args": order_values,
        }])
        return next(iter(orders.values()))

    # ------------------------------------------------------------------
    # Nastavení pokladny
    # ------------------------------------------------------------------

    def test_eic_prefilled_from_certificate(self):
        self.assertEqual(self.config.eet_eic, "CZ8551015704")
        self.assertEqual(self.config.eet_url, eet_soap.URL_PLAYGROUND)

    def test_certificate_issuer(self):
        """Testovací certifikát FS je z CA playgroundu; na produkci pokladna upozorní."""
        self.assertEqual(self.config.eet_certificate_issuer, "playground")
        self.assertFalse(self.config.eet_certificate_warning)
        self.config.eet_environment = "production"
        self.assertEqual(self.config.eet_certificate_issuer, "playground")
        self.assertIn("odmítne", self.config.eet_certificate_warning)

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
        cash = self.config.payment_method_ids.filtered(lambda method: method.type == "cash")[:1]
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order(lines=[(self.product, -1)], payments=[(cash, -45.0)])
            self.env["pos.order"].eet_send_now([order.id])
        self.assertEqual(order.eet_trzba_ids.celk_trzba, -45.0)
        self.assertIn(b'celk_trzba="-45.00"', server.requests[0][1])

    # ------------------------------------------------------------------
    # Celková částka = skutečně přijatá (metodika FS „Popis položek datové zprávy“ 1.0, kap. 12)
    # ------------------------------------------------------------------

    def test_split_payment_is_one_trzba(self):
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order(payments=[(self.cash, 40.0), (self.card, 50.0)])
        self.assertEqual(len(order.eet_trzba_ids), 1, "platba více způsoby = jedna zpráva")
        self.assertEqual(order.eet_trzba_ids.celk_trzba, 90.0)

    def test_change_is_not_received(self):
        """Zákazník dá 100 Kč, dostane 10 Kč zpět: Odoo vrácené peníze ukládá jako zápornou platbu."""
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order(payments=[(self.cash, 100.0)], amount_return=-10.0)
        self.assertEqual(order.payment_ids.filtered("is_change").amount, -10.0)
        self.assertEqual(order.eet_trzba_ids.celk_trzba, 90.0)

    def test_cash_rounding_records_received_amount(self):
        """81,60 Kč: hotově se přijme 82 Kč a eviduje 82 Kč, kartou se eviduje 81,60 Kč."""
        with patch(POST_TARGET, FakeServer()):
            cash_order = self._create_paid_order(lines=[(self.fruit, 2)], payments=[(self.cash, 82.0)])
            card_order = self._create_paid_order(lines=[(self.fruit, 2)], payments=[(self.card, 81.60)])
        self.assertAlmostEqual(cash_order.amount_total, 81.60)
        self.assertEqual(cash_order.eet_trzba_ids.celk_trzba, 82.0)
        self.assertEqual(card_order.eet_trzba_ids.celk_trzba, 81.60)

    def test_porad_cis_gaps_and_length(self):
        """Řada pořadových čísel smí mít mezery (metodika FS kap. 10), délka max. 25 znaků (rozhraní v1.2a)."""
        with patch(POST_TARGET, FakeServer()):
            first = self._create_paid_order()
            free = self._create_paid_order(lines=[(self.free_product, 1)], payments=[])
            last = self._create_paid_order()
        self.assertFalse(free.eet_trzba_ids, "objednávka zdarma číslo spotřebuje, ale neeviduje se")
        self.assertEqual(first.eet_trzba_ids.porad_cis, first.pos_reference)
        self.assertEqual(last.eet_trzba_ids.porad_cis, last.pos_reference)
        self.assertNotEqual(first.pos_reference, last.pos_reference)
        last.pos_reference = "Obchůdek/2027/" + "1" * 20
        self.assertEqual(last._eet_porad_cis(), "Obchudek/2027/11111111111")
        self.assertTrue(eet_soap.RE_PORAD_CIS.match(last._eet_porad_cis()))

    # ------------------------------------------------------------------
    # Dárkové poukazy a e-peněženka (pos_loyalty)
    # ------------------------------------------------------------------

    def _gift_card_program(self):
        if "reward_id" not in self.env["pos.order.line"]._fields:
            self.skipTest("bez modulu pos_loyalty")
        gift_card = self.create_product("Dárkový poukaz", self.categ_basic, 500.0)
        program = self.env["loyalty.program"].create({
            "name": "Dárkové poukazy",
            "program_type": "gift_card",
            "applies_on": "future",
            "trigger": "auto",
            "pos_ok": True,
            "rule_ids": [Command.create({
                "product_ids": [Command.set(gift_card.ids)],
                "reward_point_amount": 1,
                "reward_point_mode": "money",
                "reward_point_split": True,
            })],
            "reward_ids": [Command.create({
                "reward_type": "discount",
                "discount_mode": "per_point",
                "discount": 1,
                "discount_applicability": "order",
                "required_points": 1,
            })],
        })
        return gift_card, program.reward_ids

    def _reward_line(self, reward, amount):
        """Řádek „platba poukazem“ jako z pokladny; poukaz s 500 Kč se založí předem."""
        card = self.env["loyalty.card"].create({"program_id": reward.program_id.id, "points": 500})
        return {
            "product": reward.discount_line_product_id,
            "quantity": 1,
            "reward_id": reward.id,
            "card_id": card.id,
            "is_reward_line": True,
            "price_unit": amount,
            "price_subtotal": amount,
            "price_subtotal_incl": amount,
        }

    def test_gift_card_sale_is_urceno_cerp_zuct(self):
        gift_card, _reward = self._gift_card_program()
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order(lines=[(gift_card, 1), (self.product, 1)], payments=[(self.cash, 545.0)])
            self.env["pos.order"].eet_send_now([order.id])
        trzba = order.eet_trzba_ids
        self.assertEqual(trzba.celk_trzba, 545.0)
        self.assertEqual(trzba.urceno_cerp_zuct, 500.0)
        self.assertFalse(trzba.cerp_zuct)
        self.assertIn(b'urceno_cerp_zuct="500.00"', server.requests[0][1])
        self.assertNotIn(b"cerp_zuct=\"0", server.requests[0][1], "nulové položky se neuvádějí")

    def test_paid_fully_by_gift_card(self):
        """Celý nákup poukazem: peníze nepřišly žádné, a přesto je to evidovaná tržba (čerpání)."""
        _gift_card, reward = self._gift_card_program()
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order(lines=[(self.product, 2), self._reward_line(reward, -90.0)], payments=[])
            self.env["pos.order"].eet_send_now([order.id])
        trzba = order.eet_trzba_ids
        self.assertEqual(len(trzba), 1)
        self.assertEqual(trzba.celk_trzba, 90.0)
        self.assertEqual(trzba.cerp_zuct, 90.0)
        self.assertFalse(trzba.urceno_cerp_zuct)
        payload = server.requests[0][1]
        self.assertIn(b'celk_trzba="90.00"', payload)
        self.assertIn(b'cerp_zuct="90.00"', payload)
        self.assertNotIn(b"urceno_cerp_zuct", payload)

    def test_paid_partly_by_gift_card(self):
        _gift_card, reward = self._gift_card_program()
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order(
                lines=[(self.product, 2), self._reward_line(reward, -50.0)], payments=[(self.cash, 40.0)]
            )
        self.assertEqual(order.eet_trzba_ids.celk_trzba, 90.0, "40 Kč hotově + 50 Kč z poukazu")
        self.assertEqual(order.eet_trzba_ids.cerp_zuct, 50.0)

    def test_build_error_is_not_a_send_attempt(self):
        """Když zprávu nejde sestavit, nic neodešlo: první skutečné odeslání má prvni_zaslani=true."""
        server = FakeServer(RESPONSE_POK)
        with patch(POST_TARGET, server):
            order = self._create_paid_order()
            trzba = order.eet_trzba_ids
            with patch.object(type(trzba), "_build_envelope", side_effect=UserError("chybí certifikát")):
                trzba._send()
            self.assertEqual(trzba.state, "error")
            self.assertEqual(trzba.attempts, 0)
            trzba.action_send()
        self.assertEqual(trzba.state, "sent")
        self.assertIn(b'prvni_zaslani="true"', server.requests[0][1])

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

    # ------------------------------------------------------------------
    # Účtenka: Odoo 20 ji vykresluje ze serverové šablony point_of_sale.pos_order_receipt
    # (backend i pokladna); tady se ověřuje serverová strana.
    # ------------------------------------------------------------------

    def _receipt_html(self, order):
        return str(order.order_receipt_generate_html())

    def test_receipt_shows_pok(self):
        with patch(POST_TARGET, FakeServer(RESPONSE_POK)):
            order = self._create_paid_order()
            self.env["pos.order"].eet_send_now([order.id])
        html = self._receipt_html(order)
        self.assertIn("EET – tržba zaevidována", html)
        self.assertIn(f"POK: {POK}", html)
        self.assertIn("EIČ: CZ8551015704", html)
        self.assertIn("pokladna: TEST-1", html)
        self.assertIn("jednotka: 21", html)
        self.assertIn("TESTOVACÍ REŽIM – POK NENÍ PLATNÝ", html, "playground se na účtence označí")

    def test_receipt_production_has_no_test_note(self):
        self.config.eet_environment = "production"
        with patch(POST_TARGET, FakeServer(RESPONSE_POK)):
            order = self._create_paid_order()
            self.env["pos.order"].eet_send_now([order.id])
        html = self._receipt_html(order)
        self.assertIn(f"POK: {POK}", html)
        self.assertNotIn("TESTOVACÍ REŽIM", html)

    def test_receipt_offline(self):
        with patch(POST_TARGET, FakeServer(requests.ConnectionError("offline"))):
            order = self._create_paid_order()
            self.env["pos.order"].eet_send_now([order.id])
        html = self._receipt_html(order)
        self.assertIn("EET – tržba evidována v režimu offline", html)
        self.assertNotIn("POK:", html)

    def test_receipt_overeni(self):
        self.config.write({"eet_environment": "production", "eet_overeni": True})
        with patch(POST_TARGET, FakeServer(RESPONSE_OVERENI_OK)):
            order = self._create_paid_order()
            self.env["pos.order"].eet_send_now([order.id])
        html = self._receipt_html(order)
        self.assertIn("EET – ověřovací mód, tržba nebyla zaevidována", html)
        self.assertIn("TESTOVACÍ REŽIM – POK NENÍ PLATNÝ", html, "ověřovací mód se označí i na produkci")

    def test_receipt_without_eet(self):
        self.config.eet_enabled = False
        with patch(POST_TARGET, FakeServer()):
            order = self._create_paid_order()
        html = self._receipt_html(order)
        self.assertNotIn("EIČ:", html)
        self.assertNotIn("EET –", html)

    # ------------------------------------------------------------------
    # Data modulu
    # ------------------------------------------------------------------

    def test_playground_certificate_data(self):
        """Datový soubor s testovacím certifikátem (Odoo 20: type="bytes") se načetl a dá se použít."""
        # Certifikát patří hlavní firmě, testovací uživatel je v jiné.
        certificate = self.env.ref("cz_eet.certificate_playground_cz8551015704").sudo()
        self.assertEqual(certificate.scope, "eet")
        self.assertEqual(certificate.subject_common_name, "CZ8551015704")
        self.assertFalse(certificate.loading_error)
        self.assertTrue(certificate.private_key_id, "z PKCS#12 se musí vytáhnout soukromý klíč")
