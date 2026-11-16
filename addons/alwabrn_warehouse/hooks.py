import logging

from odoo.tools.misc import file_open

from .tools import catalog_reader

_logger = logging.getLogger(__name__)

UOM_XMLIDS = {
    "EA": "uom.product_uom_unit", "KM": "uom.product_uom_km", "M": "uom.product_uom_meter",
    "KIT": "alwabrn_warehouse.uom_kit", "R": "alwabrn_warehouse.uom_roll", "DRUM": "alwabrn_warehouse.uom_drum",
}


def import_catalog(env, content):
    """Import the SEC materials workbook. Idempotent: existing SEC codes are
    never duplicated. Uncertain records are imported *as is* and flagged."""
    result = catalog_reader.read_catalog(content)
    Product = env["product.template"].sudo()
    Categ = env["product.category"].sudo()
    root = env.ref("alwabrn_warehouse.categ_sec_materials")
    uoms = {code: env.ref(xmlid) for code, xmlid in UOM_XMLIDS.items()}
    categs = {}
    for name in result["categories"]:
        categ = Categ.search([("name", "=", name), ("parent_id", "=", root.id)], limit=1)
        categs[name] = categ or Categ.create({"name": name, "parent_id": root.id})
    stats = {"created": 0, "existing": 0, "flagged": 0, "rejected": 0, "categories": len(categs)}
    existing = set(Product.with_context(active_test=False).search(
        [("alw_sec_code", "!=", False)]).mapped("alw_sec_code"))
    vals_list = []
    for rec in result["records"]:
        errors = [i for i in rec["issues"] if i[0] == "error"]
        if errors:
            stats["rejected"] += 1
            continue
        if rec["sec_code"] in existing:
            stats["existing"] += 1
            continue
        existing.add(rec["sec_code"])
        warnings = [i[2] for i in rec["issues"]]
        if warnings:
            stats["flagged"] += 1
        vals_list.append({
            "name": rec["description"].strip(), "alw_sec_code": rec["sec_code"], "default_code": rec["sec_code"],
            "alw_is_material": True, "type": "consu", "is_storable": True,
            "categ_id": (categs[rec["category"]] if rec["category"] else root).id,
            "uom_id": uoms[rec["unit"]].id, "alw_reference_price": rec["price"],
            "standard_price": rec["price"], "list_price": 0.0, "sale_ok": False, "purchase_ok": False,
            "alw_source_row": rec["row"], "alw_source_formula": rec["extra_formula"],
            "alw_needs_verification": bool(warnings),
            "alw_verification_note": "\n".join(warnings) or False,
        })
    for i in range(0, len(vals_list), 200):
        Product.create(vals_list[i:i + 200])
    stats["created"] = len(vals_list)
    _logger.info("Al Wabran catalog import: %s", stats)
    return stats


def setup_units(env):
    """SEC catalog units; km is archived by default in Odoo and must be active."""
    for code, xmlid in UOM_XMLIDS.items():
        env.ref(xmlid).sudo().write({"alw_code": code, "active": True})


def post_init_hook(env):
    # Arabic first: activate Arabic and make it the default for users
    lang = "ar_001"
    try:
        env["res.lang"]._activate_lang(lang)
        env["ir.module.module"].search([("state", "=", "installed")])._update_translations(lang)
        env["res.users"].with_context(active_test=False).search([("share", "=", False)]).write(
            {"lang": lang, "tz": "Asia/Riyadh"})
        env["ir.default"].set("res.partner", "lang", lang)
        env["ir.default"].set("res.partner", "tz", "Asia/Riyadh")
    except Exception as e:  # pragma: no cover - never block installation on i18n
        _logger.warning("Arabic activation skipped: %s", e)
    company = env.company
    if company.name in ("My Company", "YourCompany"):
        company.write({"name": "شركة الوبران — فرع نجران"})
    # main warehouse stock location role
    for wh in env["stock.warehouse"].search([]):
        wh.lot_stock_id.alw_role = "warehouse"
    setup_units(env)
    # The price workbook is confidential and is NOT shipped in the public repository.
    # If it was placed in data/ before installation it is imported now; otherwise the
    # administrator uploads it from «الإعدادات ← استيراد / فحص كتالوج المواد».
    try:
        with file_open("alwabrn_warehouse/data/materials_2021.xlsx", "rb") as f:
            content = f.read()
    except FileNotFoundError:
        _logger.warning("materials_2021.xlsx not found: catalog must be uploaded from the settings menu")
        return
    import_catalog(env, content)
