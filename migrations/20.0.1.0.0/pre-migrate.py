# -*- coding: utf-8 -*-
"""Migrace cz_eet 19.0 → 20.0 (před načtením dat modulu).

Odoo 20 nahradilo ``ir.model.access`` a ``ir.rule`` jedním modelem ``ir.access``
(``security/ir.access.csv``). Stará XML ID modulu (``cz_eet.access_eet_trzba_pos_*``)
v ``ir_model_data`` pořád ukazují na model ``ir.model.access``; načtení nového CSV se stejnými
ID by skončilo chybou „found record of different model ir.model.access“. Proto se tu
smažou jen tyto odkazy (záznamy XML ID, ne data) a 20 práva založí znovu z CSV.

Když převod ``ir.model.access`` → ``ir.access`` zvládne už migrace jádra (base), nic tu
nezbyde a skript nic neudělá. Stará tabulka ``ir_model_access`` zůstává (OpenUpgrade nic
nemaže), řádky v ní už nic neřídí.

Vlastní data modulu (``eet_trzba``, pole ``eet_*`` na ``pos_config`` a ``pos_order``,
rozsah certifikátu ``eet``) se mezi 19 a 20 nemění, převod nepotřebují.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'cz_eet'
           AND model IN ('ir.model.access', 'ir.rule')
        RETURNING name
        """
    )
    names = [row[0] for row in cr.fetchall()]
    if names:
        _logger.info("cz_eet: smazána XML ID starých přístupových práv (Odoo 20 je založí jako ir.access): %s", names)
