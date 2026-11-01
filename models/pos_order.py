# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import _, api, fields, models

from . import eet_soap
from .eet_trzba import STATES


class PosOrder(models.Model):
    _inherit = "pos.order"

    eet_required = fields.Boolean(related="config_id.eet_enabled", string="Podléhá EET")
    eet_trzba_ids = fields.One2many("eet.trzba", "pos_order_id", string="Tržby EET")
    eet_state = fields.Selection(STATES, string="Stav EET", compute="_compute_eet_state", store=True)
    eet_pok = fields.Char(string="POK", compute="_compute_eet_state", store=True)

    @api.depends("eet_trzba_ids.state", "eet_trzba_ids.pok")
    def _compute_eet_state(self):
        for order in self:
            trzba = order.eet_trzba_ids[:1]
            order.eet_state = trzba.state or False
            order.eet_pok = trzba.pok or False

    def _eet_amount(self):
        """Skutečně přijatá částka: součet plateb bez „zaplatit později“.

        Vrácené peníze jsou záporná platba v hotovosti, zaokrouhlení hotovosti je už v částce
        platby, takže součet je to, co zákazník opravdu zaplatil (vratka je záporná).
        """
        self.ensure_one()
        return sum(
            payment.amount for payment in self.payment_ids if payment.payment_method_id.type != "pay_later"
        )

    def _eet_voucher_amounts(self):
        """Dárkové poukazy a e-peněženka z modulu pos_loyalty: ``(urceno_cerp_zuct, cerp_zuct)``.

        Odoo je vede jako kredit v korunách použitelný na cokoli z obchodu, tedy prostředek
        k platbě v omezené síti (metodika FS „Popis položek datové zprávy“ 1.0, kap. 13–14):
        prodej poukazu nebo dobití peněženky je platba určená k čerpání, placení poukazem
        je čerpání. Obojí patří i do celkové částky tržby. Uplatnění snižuje cenu objednávky
        řádkem odměny, a proto ho v platbách nevidíme.
        """
        self.ensure_one()
        if "reward_id" not in self.lines._fields:
            return 0.0, 0.0
        programs = self.env["loyalty.program"].sudo().with_context(active_test=False).search(
            [("program_type", "in", ("gift_card", "ewallet"))]
        )
        if not programs:
            return 0.0, 0.0
        credit_products = programs.rule_ids.product_ids
        urceno = sum(
            line.price_subtotal_incl
            for line in self.lines
            if not line.reward_id and line.product_id in credit_products
        )
        cerp = -sum(line.price_subtotal_incl for line in self.lines if line.reward_id.program_id in programs)
        return self.currency_id.round(urceno), self.currency_id.round(cerp)

    def _eet_amounts(self):
        """Částky pro datovou zprávu: celková tržba a z ní hodnotové poukazy."""
        self.ensure_one()
        urceno, cerp = self._eet_voucher_amounts()
        return {
            "celk_trzba": self.currency_id.round(self._eet_amount() + cerp),
            "urceno_cerp_zuct": urceno,
            "cerp_zuct": cerp,
        }

    def _eet_porad_cis(self):
        self.ensure_one()
        for candidate in (self.pos_reference, self.name, str(self.id)):
            value = eet_soap.sanitize_identifier(candidate, 25)
            if value:
                return value
        return str(self.id)

    def _eet_create_trzba(self):
        """Založí záznam fronty pro zaplacenou objednávku (bez odeslání)."""
        self.ensure_one()
        trzba = self.env["eet.trzba"].create({
            **self.config_id._eet_trzba_values(),
            "pos_order_id": self.id,
            "porad_cis": self._eet_porad_cis(),
            "dat_trzby": self.date_order or fields.Datetime.now(),
            **self._eet_amounts(),
        })
        # Pokladna odešle tržbu hned po synchronizaci; cron je záloha pro objednávky
        # doručené dodatečně (pokladna byla offline). Minuta odstupu brání souběhu.
        cron = self.env.ref("cz_eet.ir_cron_eet_send_pending", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(fields.Datetime.now() + timedelta(minutes=1))
        return trzba

    def action_pos_order_paid(self):
        result = super().action_pos_order_paid()
        if self.config_id.eet_enabled and not self.eet_trzba_ids and self._eet_has_amount():
            self._eet_create_trzba()
        return result

    def _eet_has_amount(self):
        """Objednávka bez přijatých peněz i bez poukazů (např. ochutnávka zdarma) se neeviduje."""
        return any(not self.currency_id.is_zero(amount) for amount in self._eet_amounts().values())

    @api.model
    def eet_send_now(self, order_ids):
        """Volá pokladna po uložení objednávky: odešle čekající tržby a vrátí POK pro účtenku."""
        result = {}
        for order in self.browse(order_ids).exists():
            pending = order.eet_trzba_ids.filtered(lambda trzba: trzba.state == "to_send")
            if pending:
                pending._send()
            result[order.id] = {"eet_state": order.eet_state or False, "eet_pok": order.eet_pok or False}
        return result

    def action_eet_send(self):
        """Tlačítko na objednávce: odeslat / odeslat znovu."""
        trzby = self.eet_trzba_ids.filtered(lambda trzba: trzba.state in ("to_send", "error"))
        if not trzby and self.config_id.eet_enabled and not self.eet_trzba_ids and self.state in ("paid", "done", "invoiced"):
            trzby = self._eet_create_trzba()
        trzby.action_send()
        return True

    def action_eet_view_trzby(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Tržby EET"),
            "res_model": "eet.trzba",
            "view_mode": "list,form",
            "domain": [("pos_order_id", "=", self.id)],
            "context": {"default_pos_order_id": self.id},
        }
