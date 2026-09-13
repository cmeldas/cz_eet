# -*- coding: utf-8 -*-
import logging
from odoo.modules import module as odoo_module

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import eet_soap

_logger = logging.getLogger(__name__)

ENVIRONMENTS = [
    ("playground", "Playground (testovací prostředí)"),
    ("production", "Produkce (ostrý provoz)"),
]

STATES = [
    ("to_send", "Čeká na odeslání"),
    ("sent", "Zaevidováno (POK)"),
    ("verified", "Ověřeno (ověřovací mód, bez evidence)"),
    ("error", "Chyba"),
]

ENVIRONMENT_URLS = {
    "playground": eet_soap.URL_PLAYGROUND,
    "production": eet_soap.URL_PRODUCTION,
}


class EetTrzba(models.Model):
    """Fronta a protokol datových zpráv evidovaných tržeb (EET 2.0).

    Jeden záznam = jedna tržba. Opakované zaslání (výpadek, dočasná chyba ``-1``)
    mění jen ``uuid_zpravy``, ``dat_odesl`` a ``prvni_zaslani``; údaje tržby zůstávají,
    takže server pozná tutéž tržbu.
    """

    _name = "eet.trzba"
    _description = "EET – evidovaná tržba"
    _order = "id desc"
    _rec_name = "porad_cis"

    company_id = fields.Many2one(
        "res.company", string="Firma", required=True, default=lambda self: self.env.company, index=True
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    config_id = fields.Many2one("pos.config", string="Pokladna", required=True, ondelete="restrict", index=True)
    pos_order_id = fields.Many2one("pos.order", string="Objednávka", ondelete="set null", index=True)

    environment = fields.Selection(ENVIRONMENTS, string="Prostředí", required=True, readonly=True)
    overeni = fields.Boolean(string="Ověřovací mód", readonly=True)
    eic_popl = fields.Char(string="EIČ poplatníka", required=True, readonly=True)
    id_jednotky = fields.Integer(string="Evidenční jednotka", required=True, readonly=True)
    id_pokl = fields.Char(string="Označení pokladny", required=True, readonly=True)
    porad_cis = fields.Char(string="Pořadové číslo tržby", required=True, readonly=True, index=True)
    dat_trzby = fields.Datetime(string="Datum a čas tržby", required=True, readonly=True)
    celk_trzba = fields.Monetary(string="Celková částka", required=True, readonly=True, currency_field="currency_id")

    state = fields.Selection(STATES, string="Stav", default="to_send", required=True, index=True, copy=False)
    pok = fields.Char(string="POK", readonly=True, copy=False, help="Potvrzovací kód přidělený správcem daně.")
    test = fields.Boolean(string="Odpověď z neprodukčního prostředí", readonly=True)
    uuid_zpravy = fields.Char(string="UUID poslední zprávy", readonly=True, copy=False)
    attempts = fields.Integer(string="Počet pokusů", default=0, readonly=True, copy=False)
    dat_odesl = fields.Datetime(string="Poslední odeslání", readonly=True, copy=False)
    dat_prij = fields.Datetime(string="Přijetí správcem daně", readonly=True, copy=False)
    response_code = fields.Char(string="Kód odpovědi", readonly=True, copy=False, help="Kód chyby ze zprávy Chyba; 0 = ověřovací mód proběhl bez formálních chyb.")
    error_text = fields.Char(string="Poslední chyba", readonly=True, copy=False)
    warnings = fields.Text(string="Varování", readonly=True, copy=False)
    transaction_id = fields.Char(string="X-Global-Transaction-Id", readonly=True, copy=False)
    request_xml = fields.Text(string="Poslední odeslaná zpráva", readonly=True, copy=False)
    response_xml = fields.Text(string="Poslední odpověď", readonly=True, copy=False)

    @api.depends("porad_cis", "config_id.name")
    def _compute_display_name(self):
        for trzba in self:
            trzba.display_name = f"{trzba.porad_cis} ({trzba.config_id.name})"

    # ------------------------------------------------------------------
    # Sestavení zprávy
    # ------------------------------------------------------------------

    def _get_url(self):
        self.ensure_one()
        return ENVIRONMENT_URLS[self.environment]

    def _get_timeout(self):
        value = self.env["ir.config_parameter"].sudo().get_param("cz_eet.timeout", str(eet_soap.DEFAULT_TIMEOUT))
        try:
            return max(float(value), 2.0)
        except ValueError:
            return eet_soap.DEFAULT_TIMEOUT

    def _prepare_message_values(self, uuid_zpravy, first_attempt, sent_at):
        self.ensure_one()
        return {
            "uuid_zpravy": uuid_zpravy,
            "dat_odesl": eet_soap.format_datetime(sent_at),
            "prvni_zaslani": first_attempt,
            "overeni": self.overeni,
            "eic_popl": self.eic_popl,
            "id_jednotky": self.id_jednotky,
            "id_pokl": self.id_pokl,
            "porad_cis": self.porad_cis,
            "dat_trzby": eet_soap.format_datetime(self.dat_trzby),
            "celk_trzba": eet_soap.format_amount(self.celk_trzba),
        }

    def _build_envelope(self, uuid_zpravy, first_attempt, sent_at):
        """Podepsaná obálka; při chybě konfigurace nebo XSD vyhodí UserError."""
        self.ensure_one()
        certificate = self.config_id.eet_certificate_id.sudo()
        if not certificate:
            raise UserError(_("Pokladna %s nemá nastavený pokladní certifikát EET.", self.config_id.name))
        trzba = eet_soap.build_trzba(self._prepare_message_values(uuid_zpravy, first_attempt, sent_at))
        errors = eet_soap.xsd_errors(trzba)
        if errors:
            raise UserError(_("Zpráva neodpovídá XSD: %s", "; ".join(errors)))

        def sign(data):
            return certificate._sign(data, hashing_algorithm="sha256", formatting=None)

        try:
            envelope = eet_soap.build_signed_envelope(
                trzba, certificate._get_der_certificate_bytes(formatting=None), sign
            )
        except ValueError as error:
            raise UserError(str(error)) from error
        if len(envelope) > eet_soap.MAX_MESSAGE_BYTES:
            raise UserError(_("Zpráva je příliš velká (%s B, limit 12 kB).", len(envelope)))
        return envelope

    # ------------------------------------------------------------------
    # Odesílání
    # ------------------------------------------------------------------

    def _lock(self, skip_locked=False):
        """Zamkne záznamy ve stavu „čeká na odeslání" proti souběžnému odeslání.

        Pokladna volá odeslání hned po synchronizaci objednávky a zároveň může běžet cron;
        ``skip_locked`` (cron) zamčené záznamy přeskočí, pokladna na zámek počká a po jeho
        uvolnění vidí už čerstvý stav.
        """
        if not self:
            return self
        self.flush_recordset(["state"])
        query = "SELECT id FROM eet_trzba WHERE id IN %s AND state = 'to_send' FOR UPDATE"
        if skip_locked:
            query += " SKIP LOCKED"
        self.env.cr.execute(query, [tuple(self.ids)])
        ids = [row[0] for row in self.env.cr.fetchall()]
        locked = self.browse(ids)
        locked.invalidate_recordset()
        return locked

    def _send(self, skip_locked=False):
        """Odešle všechny čekající záznamy; chyby zapisuje do záznamů, nevyhazuje."""
        for trzba in self._lock(skip_locked=skip_locked):
            trzba._send_one()
        return True

    def _send_one(self):
        self.ensure_one()
        first_attempt = self.attempts == 0
        uuid_zpravy = eet_soap.new_uuid()
        sent_at = fields.Datetime.now()
        try:
            envelope = self._build_envelope(uuid_zpravy, first_attempt, sent_at)
        except UserError as error:
            self.write({"state": "error", "error_text": str(error), "attempts": self.attempts + 1})
            _logger.warning("cz_eet: tržba %s nejde sestavit: %s", self.display_name, error)
            return False

        self.write({
            "uuid_zpravy": uuid_zpravy,
            "dat_odesl": sent_at,
            "attempts": self.attempts + 1,
            "request_xml": envelope.decode("utf-8"),
            "response_xml": False,
            "transaction_id": False,
        })
        try:
            response = eet_soap.post_envelope(self._get_url(), envelope, self._get_timeout())
        except requests.RequestException as error:
            self.write({"error_text": _("Spojení se nezdařilo: %s", " ".join(str(error).split())[:500])})
            _logger.info("cz_eet: tržba %s neodeslána (síť): %s", self.display_name, error)
            return False

        self.write({
            "response_xml": response.text,
            "transaction_id": response.headers.get("X-Global-Transaction-Id"),
        })
        try:
            parsed = eet_soap.parse_response(response.content)
        except (ValueError, SyntaxError) as error:
            temporary = response.status_code >= 500
            self.write({
                "state": "to_send" if temporary else "error",
                "error_text": _("Nesrozumitelná odpověď (HTTP %s): %s", response.status_code, error),
            })
            return False
        return self._apply_response(parsed, response.status_code)

    def _apply_response(self, parsed, status_code):
        self.ensure_one()
        warnings = "\n".join(f"{code}: {text}" for code, text in parsed["varovani"]) or False
        if parsed["fault"]:
            # SOAP Fault: server zprávu vůbec nezpracoval. 5xx bereme jako dočasné.
            self.write({
                "state": "to_send" if status_code >= 500 else "error",
                "error_text": _("SOAP Fault (HTTP %s): %s", status_code, parsed["fault"][:400]),
            })
            return False
        if parsed["pok"]:
            self.write({
                "state": "sent",
                "pok": parsed["pok"],
                "test": parsed["test"],
                "dat_prij": parsed["dat_prij"],
                "response_code": False,
                "error_text": False,
                "warnings": warnings,
            })
            return True
        code = parsed["kod"]
        values = {
            "response_code": code,
            "test": parsed["test"],
            "warnings": warnings,
            "error_text": f"{code}: {parsed['chyba']}" if parsed["chyba"] else code,
        }
        if code == "0":
            values.update({"state": "verified", "dat_prij": parsed["dat_odmit"] or parsed["dat_prij"], "error_text": False})
            self.write(values)
            return True
        values["state"] = "to_send" if code == "-1" else "error"
        self.write(values)
        _logger.warning("cz_eet: tržba %s odmítnuta, kód %s: %s", self.display_name, code, parsed["chyba"])
        return False

    # ------------------------------------------------------------------
    # Akce a cron
    # ------------------------------------------------------------------

    def action_send(self):
        """Tlačítko „Odeslat" – čekající záznamy odešle, chybové vrátí do fronty a odešle."""
        self.filtered(lambda t: t.state == "error").write({"state": "to_send", "error_text": False})
        return self._send()

    @api.model
    def _cron_send_pending(self, limit=200):
        pending = self.search([("state", "=", "to_send")], order="id", limit=limit)
        # V testech se commitovat nesmí (stejný vzor jako fronta e-mailů v modulu mail).
        auto_commit = not odoo_module.current_test
        for trzba in pending:
            # Zámek i odeslání po jednom: commit uvolní zámky, a tak každý záznam
            # potřebuje svůj vlastní (souběh s odesláním z pokladny řeší SKIP LOCKED).
            try:
                with self.env.cr.savepoint():
                    if trzba._lock(skip_locked=True):
                        trzba._send_one()
            except Exception:  # noqa: BLE001 – jedna vadná tržba nesmí zastavit ostatní
                _logger.exception("cz_eet: odeslání tržby %s selhalo", trzba.display_name)
                continue
            if auto_commit:
                self.env.cr.commit()
        return True
