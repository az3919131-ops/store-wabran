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
    # cable lengths are kept in km in the SEC catalog: 3 decimals = exact to the metre
    precision = env.ref("uom.decimal_product_uom").sudo()
    if precision.digits < 3:
        precision.digits = 3


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


SEED_KEY = "OPENING-STOCK-20261116"


def load_opening_stock(env, data, stock_file=None, stock_filename="حصر مواد الإنشاءات.xlsx"):
    """Post the warehouse count as the approved opening balance (once).

    ``data`` is the reviewed JSON produced from the count workbook. Codes that
    are not in the unified catalog are created as materials flagged for
    verification; nothing is posted twice (idempotency key)."""
    import base64
    import json
    Tx = env["alwabrn.transaction"].sudo()
    setup_units(env)
    if Tx.search_count([("idempotency_key", "=", SEED_KEY)]):
        _logger.info("Opening stock already loaded (%s)", SEED_KEY)
        return {"skipped": True}
    Product = env["product.template"].sudo()
    Categ = env["product.category"].sudo()
    root = env.ref("alwabrn_warehouse.categ_sec_materials")
    extra = Categ.search([("name", "=", "مواد خارج الكتالوج الموحد — تحتاج تحقق")], limit=1) or \
        Categ.create({"name": "مواد خارج الكتالوج الموحد — تحتاج تحقق", "parent_id": root.id})
    m = env.ref("uom.product_uom_meter")
    ea = env.ref("uom.product_uom_unit")
    created = []
    lines = []
    for item in data["lines"]:
        tmpl = Product.with_context(active_test=False).search([("alw_sec_code", "=", item["code"])], limit=1)
        if not tmpl:
            tmpl = Product.create({
                "name": "%s (من حصر المستودع)" % (item.get("desc") or item["code"]),
                "alw_sec_code": item["code"], "default_code": item["code"], "alw_is_material": True,
                "type": "consu", "is_storable": True, "categ_id": extra.id,
                "uom_id": (m if item["uom"] == "M" else ea).id, "sale_ok": False, "purchase_ok": False,
                "alw_needs_verification": True,
                "alw_verification_note": "الكود غير موجود في ملف المواد الموحدة 2021. أضيف من حصر مواد الإنشاءات "
                                         "بالمستودع؛ يرجى التحقق من الوصف والوحدة والسعر.\nالمصدر: %s" % "؛ ".join(item["srcs"]),
            })
            created.append(item["code"])
        product = tmpl.product_variant_id
        # metres for cables (approved conversion to the catalog unit), otherwise the material's own unit:
        # a counted piece of a KIT material is one kit.
        uom = m if item["uom"] == "M" else product.uom_id
        if not uom._has_common_reference(product.uom_id):
            raise ValueError("Unit %s not convertible for %s" % (uom.name, item["code"]))
        note = "؛ ".join(item.get("notes") or [])
        lines.append((0, 0, {"product_id": product.id, "quantity": item["qty"], "uom_id": uom.id,
                             "note": note[:250] or False}))
    docs = env["alwabrn.source.document"].sudo()
    if stock_file:
        docs = docs.create({"name": stock_filename, "datas": base64.b64encode(stock_file),
                            "channel": "excel", "register_type": "physical"})
    review = data.get("review") or []
    tx = Tx.create({
        "tx_type": "opening", "idempotency_key": SEED_KEY,
        "document_ref": "حصر مواد الإنشاءات بالمستودع %s" % data.get("date", ""),
        "reason": "رصيد افتتاحي من حصر مواد الإنشاءات بالمستودع (كل Drum = %s م)" % data.get("drum_m", 500),
        "note": "بنود لم تُضف وتحتاج قراراً:\n" + "\n".join(
            "- %s (%s): %s" % (r["code"], r["src"], r["why"]) for r in review) if review else False,
        "source_document_ids": [(6, 0, docs.ids)],
        "line_ids": lines,
    })
    tx._check_ready()
    tx._post()
    env["alwabrn.approval"].sudo().log(tx, "import", "تحميل الرصيد الافتتاحي من حصر المستودع", {
        "lines": len(lines), "new_materials": created, "not_loaded": review})
    _logger.info("Opening stock loaded: %s lines, %s new materials, %s rows for review",
                 len(lines), len(created), len(review))
    return {"lines": len(lines), "new_materials": len(created), "review": len(review), "tx": tx.name}


def load_seed_files(env, folder, key):
    """Decrypt the deployment seed files (Fernet) and load catalog + opening stock."""
    import json
    import os
    from cryptography.fernet import Fernet
    f = Fernet(key.encode())
    res = {}
    cat_path = os.path.join(folder, "materials_2021.xlsx.enc")
    if os.path.exists(cat_path):
        res["catalog"] = import_catalog(env, f.decrypt(open(cat_path, "rb").read()))
    stock_path = os.path.join(folder, "opening_stock.json.enc")
    if os.path.exists(stock_path):
        data = json.loads(f.decrypt(open(stock_path, "rb").read()).decode("utf-8"))
        xlsx_path = os.path.join(folder, "stock_count.xlsx.enc")
        raw = f.decrypt(open(xlsx_path, "rb").read()) if os.path.exists(xlsx_path) else None
        res["opening"] = load_opening_stock(env, data, raw)
    return res
