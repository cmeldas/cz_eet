# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import eet_soap
from .eet_trzba import ENVIRONMENTS, ENVIRONMENT_URLS


class PosConfig(models.Model):
    _inherit = "pos.config"

    eet_enabled = fields.Boolean(
        string="Evidence tržeb (EET)",
        help="Po zaplacení objednávky odeslat datovou zprávu evidované tržby správci daně.",
    )
    eet_environment = fields.Selection(
        ENVIRONMENTS,
        string="Prostředí EET",
        default="playground",
        required=True,
        help="Playground je testovací prostředí Finanční správy: vrací fiktivní POK (končí „-ff“) "
        "a tržby neeviduje. Produkce je ostrý provoz.",
    )
    eet_overeni = fields.Boolean(
        string="Ověřovací mód",
        help="Zprávy se posílají s příznakem overeni=true: server je jen zvaliduje a nic nezaeviduje "
        "(vhodné pro zkoušku nastavení; na produkci povoleno od 1. 11. 2026).",
    )
    eet_certificate_id = fields.Many2one(
        "certificate.certificate",
        string="Pokladní certifikát",
        check_company=True,
        domain="[('scope', 'in', ['eet', 'general'])]",
        help="Certifikát z CA EET (PKCS#12 z portálu MOJE daně / DIS+). Pro playground použij "
        "testovací certifikát Finanční správy, který modul instaluje.",
    )
    eet_certificate_valid_until = fields.Datetime(related="eet_certificate_id.date_end", string="Certifikát platí do")
    eet_certificate_issuer = fields.Selection(
        [*ENVIRONMENTS, ("unknown", "Jiný vydavatel (ne CA EET)")],
        string="Certifikát vydala CA EET",
        compute="_compute_eet_certificate_issuer",
        help="Ověřeno podle certifikátů CA EET Finanční správy (produkční a playground) přibalených k modulu.",
    )
    eet_certificate_warning = fields.Char(string="Upozornění k certifikátu", compute="_compute_eet_certificate_issuer")
    eet_eic = fields.Char(
        string="EIČ poplatníka",
        size=12,
        compute="_compute_eet_eic",
        store=True,
        readonly=False,
        help="Evidenční identifikační číslo ve tvaru CZ + 8–10 číslic. Musí se shodovat "
        "s certifikátem (jeho CN), proto se z něj předvyplňuje.",
    )
    eet_id_jednotky = fields.Integer(
        string="Evidenční jednotka",
        help="Číslo evidenční jednotky přidělené v DIS+ (1–999 999 999). Prodej mimo areál "
        "(trhy) má vlastní jednotku „činnost mimo provozovnu“.",
    )
    eet_id_pokl = fields.Char(
        string="Označení pokladny",
        size=20,
        compute="_compute_eet_id_pokl",
        store=True,
        readonly=False,
        help="Označení pokladního zařízení (id_pokl), max. 20 znaků bez diakritiky; "
        "unikátní v rámci evidenční jednotky.",
    )
    eet_url = fields.Char(string="Adresa služby EET", compute="_compute_eet_url")

    @api.depends("eet_certificate_id.subject_common_name")
    def _compute_eet_eic(self):
        for config in self:
            common_name = (config.eet_certificate_id.subject_common_name or "").strip()
            if eet_soap.RE_EIC.match(common_name):
                config.eet_eic = common_name
            elif not config.eet_eic:
                config.eet_eic = False

    @api.depends("eet_certificate_id.pem_certificate", "eet_environment")
    def _compute_eet_certificate_issuer(self):
        environments = dict(ENVIRONMENTS)
        for config in self:
            certificate = config.eet_certificate_id.sudo()
            if not certificate:
                config.eet_certificate_issuer = config.eet_certificate_warning = False
                continue
            try:
                certificate_der = certificate._get_der_certificate_bytes(formatting=None)
            except (AttributeError, TypeError, ValueError):
                # PKCS#12 se nepodařilo načíst (špatné heslo, poškozený soubor): pem_certificate je prázdný
                certificate_der = b""
            issuer = eet_soap.certificate_environment(certificate_der) or "unknown"
            config.eet_certificate_issuer = issuer
            if issuer == "unknown":
                config.eet_certificate_warning = _(
                    "Certifikát nevydala certifikační autorita EET, správce daně s ním podepsané zprávy odmítne."
                )
            elif issuer != config.eet_environment:
                config.eet_certificate_warning = _(
                    "Certifikát je pro %(issuer)s, pokladna ale posílá do prostředí %(environment)s. "
                    "Správce daně zprávy odmítne.",
                    issuer=environments[issuer],
                    environment=environments[config.eet_environment],
                )
            else:
                config.eet_certificate_warning = False

    @api.depends("name")
    def _compute_eet_id_pokl(self):
        for config in self:
            if not config.eet_id_pokl:
                config.eet_id_pokl = eet_soap.sanitize_identifier(config.name, 20) or False

    @api.depends("eet_environment")
    def _compute_eet_url(self):
        for config in self:
            config.eet_url = ENVIRONMENT_URLS.get(config.eet_environment, False)

    @api.constrains("eet_enabled", "eet_certificate_id", "eet_eic", "eet_id_jednotky", "eet_id_pokl")
    def _check_eet_settings(self):
        for config in self.filtered("eet_enabled"):
            if not config.eet_certificate_id:
                raise ValidationError(_("EET (%s): chybí pokladní certifikát.", config.name))
            if not eet_soap.RE_EIC.match(config.eet_eic or ""):
                raise ValidationError(_("EET (%s): EIČ musí mít tvar CZ + 8 až 10 číslic.", config.name))
            if not 1 <= (config.eet_id_jednotky or 0) <= 999_999_999:
                raise ValidationError(_("EET (%s): evidenční jednotka musí být číslo 1–999 999 999.", config.name))
            if not eet_soap.RE_ID_POKL.match(config.eet_id_pokl or ""):
                raise ValidationError(
                    _("EET (%s): označení pokladny smí mít 1–20 znaků a–z, A–Z, 0–9 . , : ; / # - _ mezera.", config.name)
                )

    def _eet_trzba_values(self):
        """Snímek nastavení pro nový záznam fronty (prostředí a mód se mohou později změnit)."""
        self.ensure_one()
        return {
            "company_id": self.company_id.id,
            "config_id": self.id,
            "environment": self.eet_environment,
            "overeni": self.eet_overeni,
            "eic_popl": self.eet_eic,
            "id_jednotky": self.eet_id_jednotky,
            "id_pokl": self.eet_id_pokl,
        }

    def action_eet_test_connection(self):
        """Pošle zkušební tržbu 0,00 Kč v ověřovacím módu a ukáže výsledek.

        Nic se neeviduje ani na produkci (overeni=true). Záznam zůstane ve frontě
        jako protokol se stavem „Ověřeno“ nebo „Chyba“.
        """
        self.ensure_one()
        if not self.eet_certificate_id:
            raise UserError(_("Nejdřív vyber pokladní certifikát."))
        now = fields.Datetime.now()
        trzba = self.env["eet.trzba"].create({
            **self._eet_trzba_values(),
            "overeni": True,
            "porad_cis": eet_soap.sanitize_identifier("TEST-" + now.strftime("%Y%m%d-%H%M%S"), 25),
            "dat_trzby": now,
            "celk_trzba": 0.0,
        })
        trzba._send()
        if trzba.state == "verified":
            message = _("Spojení funguje (%s). Server zprávu přijal v ověřovacím módu.", dict(ENVIRONMENTS)[self.eet_environment])
            if trzba.warnings:
                message += "\n" + _("Varování: %s", trzba.warnings)
            notification_type = "success" if not trzba.warnings else "warning"
        else:
            message = _("Test selhal: %s", trzba.error_text or trzba.state)
            notification_type = "danger"
        if self.eet_certificate_warning:
            message += "\n" + self.eet_certificate_warning
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Test EET"), "message": message, "type": notification_type, "sticky": True},
        }
