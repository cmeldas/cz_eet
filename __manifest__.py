# -*- coding: utf-8 -*-
{
    "name": "EET 2.0 pro pokladnu (CZ)",
    "summary": "Elektronická evidence tržeb 2.0 (od 1. 1. 2027) pro Odoo Pokladnu: podepsané SOAP zprávy, POK na účtence, fronta pro offline",
    "description": """
EET 2.0 pro pokladnu (CZ)
=========================

Evidence tržeb podle zákona o evidenci tržeb ve znění platném od 1. 1. 2027
(EET 2.0, rozhraní Finanční správy v4.1). Po zaplacení objednávky v pokladně
modul sestaví datovou zprávu ``<Trzba>``, podepíše ji pokladním certifikátem
(WS-Security, Exclusive C14N, RSA-SHA256), odešle na společné technické zařízení
správce daně a uloží potvrzovací kód (POK). POK, EIČ a označení pokladny se tisknou
na účtenku.

* Nastavení je **na pokladně** (``pos.config``): zapnutí evidence, prostředí
  (Playground / Produkce), ověřovací mód, pokladní certifikát, EIČ, evidenční jednotka,
  označení pokladny. Každá pokladna může mířit jinam – např. jedna do testovacího
  playgroundu a druhá do ostrého provozu.
* Tržby čekají ve frontě ``eet.trzba``; při výpadku spojení je odešle cron
  (zákon: bezodkladně po obnovení, nejpozději do 48 h). Opakované zaslání má nové
  UUID zprávy a ``prvni_zaslani="false"``.
* Storno/refundace se eviduje jako tržba se zápornou částkou.
* Údaje EET na účtence doplňuje dědění serverové šablony
  ``point_of_sale.pos_order_receipt`` (v Odoo 20 ji vykresluje pokladna i backend).
* Součástí je testovací certifikát Finanční správy pro playground (EIČ CZ8551015704).
""",
    "version": "20.0.1.0.0",
    "category": "Sales/Point of Sale",
    "author": "Matouš Čmelík",
    "website": "https://github.com/cmeldas/cz_eet",
    "license": "LGPL-3",
    "depends": ["point_of_sale", "certificate"],
    "external_dependencies": {"python": ["lxml", "cryptography", "requests"]},
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "data/certificate_playground.xml",
        "views/eet_trzba_views.xml",
        "views/pos_order_views.xml",
        "views/res_config_settings_views.xml",
        "receipt/pos_order_receipt.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "cz_eet/static/src/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
