# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pos_eet_enabled = fields.Boolean(related="pos_config_id.eet_enabled", readonly=False)
    pos_eet_environment = fields.Selection(related="pos_config_id.eet_environment", readonly=False)
    pos_eet_overeni = fields.Boolean(related="pos_config_id.eet_overeni", readonly=False)
    pos_eet_certificate_id = fields.Many2one(
        "certificate.certificate", related="pos_config_id.eet_certificate_id", readonly=False
    )
    pos_eet_certificate_valid_until = fields.Datetime(related="pos_config_id.eet_certificate_valid_until")
    pos_eet_eic = fields.Char(related="pos_config_id.eet_eic", readonly=False)
    pos_eet_id_jednotky = fields.Integer(related="pos_config_id.eet_id_jednotky", readonly=False)
    pos_eet_id_pokl = fields.Char(related="pos_config_id.eet_id_pokl", readonly=False)
    pos_eet_url = fields.Char(related="pos_config_id.eet_url")

    def action_eet_test_connection(self):
        """Uloží nastavení a pošle zkušební zprávu v ověřovacím módu."""
        self.ensure_one()
        self.execute()
        return self.pos_config_id.action_eet_test_connection()
