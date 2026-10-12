# EET 2.0 pro pokladnu (`cz_eet`)

Odoo 20 modul (větev `20.0`; verze pro Odoo 19 je na větvi `19.0`) pro **elektronickou evidenci tržeb 2.0** (zákon o evidenci tržeb ve znění
účinném od 1. 1. 2027, rozhraní Finanční správy **v4.1**). Po zaplacení objednávky
v pokladně sestaví datovou zprávu `<Trzba>`, podepíše ji pokladním certifikátem a odešle
správci daně; potvrzovací kód (**POK**) uloží k objednávce a tiskne na účtenku.

Podklady: [eet.gov.cz/pro-vyvojare](https://eet.gov.cz/cs/pro-vyvojare) – Popis rozhraní v1.2
(25. 8. 2026), `EETXMLSchema.xsd` 4.1, vzorové zprávy `CZ*.eet.v4.req.xml`, přístupové
informace k playgroundu a produkci.

## Co dělá

- **Nastavení na pokladně** (`pos.config`, Pokladna → Konfigurace → Nastavení → blok
  *Evidence tržeb (EET 2.0)*): zapnutí evidence, **prostředí** (Playground / Produkce),
  **ověřovací mód**, pokladní certifikát, EIČ, evidenční jednotka, označení pokladny.
  Každá pokladna může mířit jinam – jedna do testovacího playgroundu, druhá do ostrého provozu.
- Po zaplacení (`pos.order.action_pos_order_paid`) vznikne záznam fronty **`eet.trzba`**
  (Pokladna → Objednávky → *Evidence tržeb (EET)*). Pokladna ho hned po uložení objednávky
  nechá odeslat (RPC `pos.order.eet_send_now`) a POK vytiskne na účtenku.
- **Offline / výpadek**: tržba zůstane ve stavu *Čeká na odeslání*, účtenka dostane poznámku
  o offline režimu a záznam odešle cron *EET: odeslat čekající tržby* (každých 5 minut,
  po uložení objednávky navíc spouštěný s minutovým odstupem). Opakované zaslání má nové
  `uuid_zpravy` a `prvni_zaslani="false"`; server tutéž tržbu pozná podle
  `eic_popl|id_jednotky|id_pokl|porad_cis|dat_trzby|celk_trzba`.
- **Chyby**: dočasná chyba (`-1`), síťová chyba a SOAP Fault s HTTP 5xx se opakují
  automaticky; kritické chyby (XSD `3`, podpis `4`, EIČ `6`, velikost `7`, technická `8`)
  skončí ve stavu *Chyba* a čekají na opravu nastavení a tlačítko *Odeslat znovu*.
- **Storno / vratka** = tržba se zápornou částkou (nová zpráva, nepáruje se na původní).
- **Ověřovací mód** (`overeni="true"`): server zprávu jen zvaliduje (kód `0`), nic neeviduje,
  záznam má stav *Ověřeno*. Tlačítko *Otestovat spojení* v nastavení pošle zkušební tržbu
  0,00 Kč v ověřovacím módu a ukáže výsledek včetně varování správce daně.
- Účtenka: „EET – tržba zaevidována, POK: …“ / „ověřovací mód“ / „režim offline“,
  EIČ, označení pokladny a jednotky; na playgroundu nebo v ověřovacím módu navíc
  **TESTOVACÍ REŽIM – POK NENÍ PLATNÝ**.

## Co se posílá

| Atribut | Zdroj |
|---|---|
| `eic_popl` | `pos.config.eet_eic` (předvyplní se z CN certifikátu) |
| `id_jednotky` | `pos.config.eet_id_jednotky` (číslo z DIS+) |
| `id_pokl` | `pos.config.eet_id_pokl` (výchozí název pokladny bez diakritiky, max. 20 znaků) |
| `porad_cis` | `pos.order.pos_reference` (číslo účtenky, např. `261-1-000005`) |
| `dat_trzby` | `pos.order.date_order` převedené do `Europe/Prague` s offsetem (`+01:00`/`+02:00`) |
| `celk_trzba` | součet plateb objednávky bez metody „zaplatit později“ (vratka záporně), 2 desetinná místa |
| `overeni` | jen když je zapnutý ověřovací mód |

Zpráva bez položek, DPH, BKP a PKP – EET 2.0 je chce takto minimální. Nepovinné atributy
(`eic_poverujiciho`, `povereni_vice_popl`, `urceno_cerp_zuct`, `cerp_zuct`) modul nepoužívá.

Podpis: WS-Security, podepsán právě `soap:Body` (`wsu:Id`), Exclusive C14N s
`InclusiveNamespaces` jako ve vzorech FS, SHA-256, RSA-SHA256, certifikát
v `BinarySecurityToken`. Implementace je v `models/eet_soap.py` (čisté funkce,
`lxml` + `cryptography`, bez dalších závislostí); test `test_official_sample_verifies`
ověřuje otisk i podpis vzorové zprávy FS.

Past, na kterou jsme narazili: libxml2 nad stromem sestaveným v paměti občas
(nedeterministicky, řádově 1 z 10–200 zpráv) při exkluzivní kanonizaci nevidí deklarace
jmenných prostorů předků, takže otisk i podpis neodpovídají odeslanému XML a server vrátí
kód 4 „Neplatný podpis SOAP zprávy“. Otisk a podpis se proto počítají až nad znovu
naparsovaným dokumentem a každá obálka se před odesláním sama ověří
(`test_many_envelopes_stay_consistent`).

Ověřeno proti playgroundu FS 13. 9. 2026: ostrý mód (POK končící `-ff`), storno se zápornou
částkou, opakované zaslání, ověřovací mód (kód 0).

## Certifikáty

Pokladní certifikáty ukládá standardní modul Odoo **`certificate`** (`certificate.certificate`,
PKCS#12 + heslo, klíč se z něj vytáhne sám). Modul instaluje veřejný **testovací certifikát
Finanční správy pro playground** (EIČ `CZ8551015704`, heslo `aaaa1111`) – stačí ho vybrat
u pokladny s prostředím Playground.

Produkční certifikát (RSA 2048, platnost 366 dní) vydává CA EET v **DIS+ (MOJE daně)** →
Evidence tržeb: nejdřív založit evidenční jednotku, pak vygenerovat certifikát (možné od
1. 11. 2026). Soubor `.p12` nahrát jako nový certifikát (Nastavení → Certifikáty, rozsah
*EET – pokladní certifikát*) a vybrat u pokladny s prostředím Produkce. Datum konce platnosti
je vidět v nastavení pokladny; výměna je zatím ruční.

Adresy služby: playground `https://pg.trzbyeet.gov.cz/eet/services/EETServiceSOAP/v4`,
produkce `https://trzbyeet.gov.cz/eet/services/EETServiceSOAP/v4` (přechodný režim od
1. 11. 2026, ostré tržby od 1. 1. 2027; údržba čtvrtek 20:00–6:00).

## Nastavení pro Ranč Na Samotách

| Pokladna | Prostředí | Jednotka | `id_pokl` |
|---|---|---|---|
| Obchůdek | Produkce (do 31. 12. 2026 ověřovací mód) | provozovna Samoty (číslo z DIS+) | `Obchudek` |
| Trhy | Produkce; při vývoji Playground | „činnost mimo provozovnu“ (druhá jednotka z DIS+) | `Trhy` |

Doporučený postup: září–říjen 2026 obě pokladny proti playgroundu, od 1. 11. 2026 produkce
v ověřovacím módu s ostrým certifikátem, od 1. 1. 2027 ověřovací mód vypnout.

Časový limit odpovědi serveru je 5 s (`ir.config_parameter` `cz_eet.timeout`, minimálně 2 s –
zákon chce čekat alespoň 2 s).

## Testy

```
odoo -d <db> -i cz_eet --test-enable --test-tags /cz_eet --stop-after-init
```

Testy nepotřebují síť: HTTP volání nahrazuje falešný server, podpis se ověřuje lokálně
a proti vzorové zprávě FS. Ostrý test proti playgroundu: v nastavení pokladny zvolit
Playground + testovací certifikát a stisknout *Otestovat spojení*, případně zaplatit
objednávku – POK končí `-ff`.

## Odoo 20

Port z 19.0.1.0.0 na **20.0.1.0.0**, chování modulu se nemění (stejná data, stejné zprávy,
stejný postup v pokladně).

Co se změnilo:

- **Účtenka**: Odoo 20 už nemá OWL šablonu `point_of_sale.OrderReceipt`. Účtenku vykresluje
  serverová QWeb šablona `point_of_sale.pos_order_receipt` – backend (stažení, e-mail)
  i pokladna, která si ji při startu načte a výrazy převede z Pythonu do JS
  (`convert_python_template.js`). Blok EET je proto v `receipt/pos_order_receipt.xml`
  (dědění před `div[@name='before-footer']`) a píše se jen podmnožinou syntaxe, kterou
  převodník zvládne (`order.get('x')`, `config['x']`, `and/or/not`, porovnání, žádná volání
  metod). Data bere ze slovníků `order` a `config`, které obsahují všechna pole
  `pos.order`/`pos.config` včetně `eet_*`.
- **Přístupová práva**: `security/ir.model.access.csv` nahradil `security/ir.access.csv`
  (nový model `ir.access`, sloupec `operation`: uživatel pokladny `cru`, manažer `crud` –
  stejně jako v 19).
- **JS**: oba patche (`PosOrder.waitForPushOrder`, `OrderPaymentValidation.beforePostPushOrderResolve`)
  zůstaly, háčky v 20 existují se stejným významem; OWL komponenty modul nemá, převod na OWL 3
  se ho netýká. V 20 se `finalizeValidation` spouští na pozadí obrazovky „Amount Paid“
  a automatický tisk účtenky (`afterOrderValidation`) čeká až na `beforePostPushOrderResolve`,
  takže POK je na účtence stejně jako v 19.
- **Python**: `ir.config_parameter.get_param()` → `get_float()` (časový limit), zamykání
  fronty přes `env.execute_query(SQL(...))`; certifikát v datech `type="bytes"` místo
  `type="base64"` (binární pole v 20 drží `BinaryBytes`).
- **Pohledy**: ikony tlačítek na Material Symbols (`east`, `power`, `account_balance`),
  widget `remaining_days` → `relative_date`; adresa služby v nastavení se zalamuje.
- **Testy**: `is_cash_count` na platební metodě v 20 není (hotovost = `type == 'cash'`),
  přibyly testy serverové účtenky (POK, playground/produkce, offline, ověřovací mód,
  pokladna bez EET) a načtení testovacího certifikátu z dat modulu – celkem 38 testů.

Migrace dat: na produkci (kopie 6. 10. 2026) modul **není nainstalovaný**, převádět nic
nebylo potřeba. Kdyby se do migrace na 20 nainstaloval v 19, vlastní data (`eet_trzba`,
pole `eet_*` na `pos_config` a `pos_order`, rozsah certifikátu `eet`) se nemění.
`migrations/20.0.1.0.0/pre-migrate.py` jen smaže XML ID starých práv
(`cz_eet.access_eet_trzba_pos_*` v `ir_model_data` s modelem `ir.model.access`), aby šla
znovu založit jako `ir.access`; když je převede už migrace jádra, skript nic neudělá.
Stará tabulka `ir_model_access` zůstává (OpenUpgrade nic nemaže).

Ověřeno 11. 10. 2026 v čisté DB Odoo 20: instalace bez chyb a varování, 38/38 testů,
balíčky assetů se sestaví, v headless Edge pokladna načtená bez chyb v konzoli, platba
v hotovosti proti playgroundu FS → POK `…-ff` na účtence, simulovaný výpadek → varování
a účtenka „offline“, backend (objednávka, seznam a formulář tržeb, nastavení pokladny) bez
chyb. Přechod 19 → 20 (`-u cz_eet` z verze 19.0.1.0.0) prošel s pre-migrate skriptem.

## Licence

LGPL-3, autor Matouš Čmelík.
