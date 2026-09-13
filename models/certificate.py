# -*- coding: utf-8 -*-
from odoo import fields, models


class CertificateCertificate(models.Model):
    _inherit = "certificate.certificate"

    scope = fields.Selection(selection_add=[("eet", "EET – pokladní certifikát")])
