import base64
from datetime import timedelta

from odoo import api, fields, models

from ..tools.wo_excel import STAGE_SEL
from .common import riyadh_day_end_utc, riyadh_day_start_utc, riyadh_today

BUCKET = {"receipt": "rec", "opening": "open", "issue_site": "iss", "issue": "iss", "consume": "con",
          "return_in": "ret", "return_legacy": "ret", "return_out": "rsec", "damaged": "oth", "scrap": "oth",
          "adj_plus": "adj", "adj_minus": "adjm", "transfer": "trf"}


class AlwabrnTransactionLineWO(models.Model):
    _inherit = "alwabrn.transaction.line"

    work_order_id = fields.Many2one(related="transaction_id.work_order_id", store=True, index=True, string="أمر العمل")
    categ_id = fields.Many2one(related="product_id.categ_id", store=True, string="الفئة")


class AlwabrnExplorer(models.AbstractModel):
    """لوحة المواد وأوامر العمل: كل الأرقام من حركات Odoo المرحّلة وأرصدة stock.quant مباشرة."""
    _name = "alwabrn.explorer"
    _description = "لوحة المواد وأوامر العمل"

    @api.model
    def get_options(self):
        root = self.env.ref("alwabrn_warehouse.categ_sec_materials")
        return {
            "categories": self.env["product.category"].search_read([("parent_id", "=", root.id)], ["name"]),
            "stages": STAGE_SEL,
            "sites": self.env["alwabrn.site"].search_read([("code", "not like", "____%")], ["display_name"], limit=200),
            "tx_types": self.env["alwabrn.transaction"]._fields["tx_type"]._description_selection(self.env),
        }

    def _f(self, f):
        f = dict(f or {})
        today = riyadh_today()
        f["date_from"] = fields.Date.to_date(f.get("date_from")) or (today - timedelta(days=365))
        f["date_to"] = fields.Date.to_date(f.get("date_to")) or today
        for k in ("product_id", "categ_id", "work_order_id", "site_id"):
            f[k] = int(f[k]) if f.get(k) else False
        if f.get("sec_code") and not f["product_id"]:
            p = self.env["product.template"].alw_find_by_sec(str(f["sec_code"]).strip())
            f["product_id"] = p.id or -1
        if f.get("wo_text") and not f["work_order_id"]:
            wo = self.env["alwabrn.work.order"].search([("name", "=", str(f["wo_text"]).strip())], limit=1)
            f["work_order_id"] = wo.id or -1
        return f

    def _line_domain(self, f, with_dates=True):
        dom = [("state", "=", "posted")]
        if with_dates:
            dom += [("date", ">=", riyadh_day_start_utc(f["date_from"])), ("date", "<=", riyadh_day_end_utc(f["date_to"]))]
        if f["product_id"]:
            dom.append(("product_id", "=", f["product_id"]))
        if f["categ_id"]:
            dom.append(("categ_id", "child_of", f["categ_id"]))
        if f["work_order_id"]:
            dom.append(("work_order_id", "=", f["work_order_id"]))
        if f.get("stage"):
            dom.append(("work_order_id.stage", "=", f["stage"]))
        if f["site_id"]:
            dom.append(("site_id", "=", f["site_id"]))
        if f.get("tx_type"):
            dom.append(("tx_type", "=", f["tx_type"]))
        return dom

    @api.model
    def get_data(self, filters=None):
        f = self._f(filters)
        Line = self.env["alwabrn.transaction.line"]
        WO = self.env["alwabrn.work.order"]
        Balance = self.env["alwabrn.stock.balance"]
        Product = self.env["product.product"]
        ldom = self._line_domain(f)
        single = f["product_id"] and f["product_id"] > 0
        # ---- per material totals for the period
        per = {}
        for product, tx_type, qty, cnt in Line._read_group(ldom, ["product_id", "tx_type"], ["qty_product_uom:sum", "__count"]):
            d = per.setdefault(product.id, dict.fromkeys(("rec", "open", "iss", "con", "ret", "rsec", "oth", "adj", "adjm", "trf"), 0.0))
            d[BUCKET.get(tx_type, "oth")] += qty
        # ---- balances (all time, official quants)
        bdom = [("product_id", "=", f["product_id"])] if f["product_id"] else []
        if f["categ_id"]:
            bdom.append(("categ_id", "child_of", f["categ_id"]))
        bal = {}
        for product, role, qty, value in Balance._read_group(bdom, ["product_id", "role"], ["quantity:sum", "value:sum"]):
            d = bal.setdefault(product.id, {"warehouse": 0.0, "custody": 0.0, "damaged": 0.0, "value": 0.0})
            d[role] = qty
            d["value"] += value or 0.0
        pids = list(set(per) | set(bal))
        if f["work_order_id"] or f.get("stage") or f["site_id"] or f.get("tx_type"):
            pids = list(per)
        products = Product.browse(pids)
        rows = []
        for p in products:
            a, b = per.get(p.id, {}), bal.get(p.id, {})
            rows.append({"id": p.id, "tmpl": p.product_tmpl_id.id, "sec": p.alw_sec_code or "", "name": p.name, "uom": p.uom_id.name,
                         "warehouse": b.get("warehouse", 0.0), "custody": b.get("custody", 0.0),
                         "total": b.get("warehouse", 0.0) + b.get("custody", 0.0) + b.get("damaged", 0.0),
                         "value": round(b.get("value", 0.0), 2), "rec": a.get("rec", 0.0) + a.get("open", 0.0),
                         "iss": a.get("iss", 0.0), "con": a.get("con", 0.0), "ret": a.get("ret", 0.0), "rsec": a.get("rsec", 0.0)})
        rows.sort(key=lambda r: (-(r["iss"] + r["ret"] + r["rec"]), -r["value"], r["sec"]))
        # ---- work orders
        wdom = []
        if f["work_order_id"]:
            wdom.append(("id", "=", f["work_order_id"]))
        if f.get("stage"):
            wdom.append(("stage", "=", f["stage"]))
        if f["product_id"]:
            wdom.append("|")
            wdom.append(("material_ids.product_id", "=", f["product_id"]))
            wdom.append(("transaction_ids.line_ids.product_id", "=", f["product_id"]))
        if f.get("wo_search"):
            s = f["wo_search"]
            wdom += ["|", "|", ("name", "ilike", s), ("contractor", "ilike", s), ("status_note", "ilike", s)]
        stage_counts = {k: c for k, c in WO._read_group(wdom, ["stage"], ["__count"])}
        wo_lines = {}
        wl_dom = [d for d in self._line_domain(f, with_dates=False) if not (isinstance(d, tuple) and d[0] in ("work_order_id", "work_order_id.stage"))]
        wl_dom.append(("work_order_id", "!=", False))
        for wo, tx_type, qty, cnt in Line._read_group(wl_dom, ["work_order_id", "tx_type"], ["qty_product_uom:sum", "__count"]):
            d = wo_lines.setdefault(wo.id, {"rec": 0, "iss": 0, "con": 0, "ret": 0, "rsec": 0, "qty": {}})
            k = BUCKET.get(tx_type)
            if k in d:
                d[k] += cnt
                d["qty"][k] = d["qty"].get(k, 0.0) + qty
        stage_lbl = dict(STAGE_SEL)
        orders = WO.search(wdom, order="name desc", limit=150)
        wos = []
        for wo in orders:
            l = wo_lines.get(wo.id, {})
            q = l.get("qty", {})
            wos.append({"id": wo.id, "name": wo.name, "type": wo.type_summary or "", "stage": stage_lbl.get(wo.stage, ""),
                        "status": wo.status_note or "", "contractor": wo.contractor or "", "minutes": wo.minutes_count,
                        "rec": l.get("rec", 0), "iss": l.get("iss", 0), "con": l.get("con", 0), "ret": l.get("ret", 0),
                        "rsec": l.get("rsec", 0),
                        "q": {k: round(v, 3) for k, v in q.items()} if single else {}})
        # ---- KPIs
        tot = lambda k: sum(r[k] for r in rows)  # noqa: E731
        lines_cnt = {k: 0 for k in ("rec", "iss", "con", "ret", "rsec")}
        for tx_type, cnt in Line._read_group(ldom, ["tx_type"], ["__count"]):
            k = BUCKET.get(tx_type)
            if k == "open":
                k = "rec"
            if k in lines_cnt:
                lines_cnt[k] += cnt
        uom = products[:1].uom_id.name if single and products else ""
        fmt = lambda v: ("{:,.3f}".format(v).rstrip("0").rstrip(".") + " " + uom) if single else "{:,}".format(int(v))  # noqa: E731
        val = lambda k: tot(k) if single else lines_cnt[k]  # noqa: E731
        kpis = [
            {"key": "materials", "label": "مواد ضمن الفلتر", "display": "{:,}".format(len(rows)), "tone": "info"},
            {"key": "wh", "label": "رصيد المستودع" + ("" if single else " (قيمة مرجعية)"),
             "display": fmt(tot("warehouse")) if single else "{:,.0f} ر.س".format(sum(r["value"] for r in rows)), "tone": "info"},
            {"key": "custody", "label": "بعهدة المواقع وأوامر العمل" if single else "مواد بعهدة المواقع",
             "display": fmt(tot("custody")) if single else "{:,}".format(len([r for r in rows if r["custody"]])), "tone": "info"},
            {"key": "rec", "label": "مستلم من الكهرباء" + ("" if single else " (بنود)"), "display": fmt(val("rec")), "tone": "good"},
            {"key": "iss", "label": "مسلّم للمواقع" + ("" if single else " (بنود)"), "display": fmt(val("iss")), "tone": "warn"},
            {"key": "con", "label": "منفذ / مستهلك" + ("" if single else " (بنود)"), "display": fmt(val("con")), "tone": "info"},
            {"key": "ret", "label": "راجع للمستودع" + ("" if single else " (بنود)"), "display": fmt(val("ret")), "tone": "good"},
            {"key": "rsec", "label": "مرتجع للكهرباء" + ("" if single else " (بنود)"), "display": fmt(val("rsec")), "tone": "info"},
            {"key": "wos", "label": "أوامر عمل ضمن الفلتر", "display": "{:,}".format(sum(stage_counts.values())), "tone": "info"},
        ]
        # ---- charts
        months = {}
        for tx_type, month, qty, cnt in Line._read_group(ldom, ["tx_type", "date:month"], ["qty_product_uom:sum", "__count"]):
            k = BUCKET.get(tx_type)
            if k in ("iss", "ret", "rec", "open", "con"):
                k = "rec" if k == "open" else k
                m = months.setdefault(month.strftime("%Y-%m"), {"rec": 0, "iss": 0, "ret": 0, "con": 0})
                m[k] += qty if single else cnt
        mk = sorted(months)
        top_iss = sorted(rows, key=lambda r: -r["iss"])[:10] if single else None
        top_mat = Line._read_group(ldom + [("tx_type", "in", ("issue_site", "issue"))], ["product_id"], ["__count"],
                                   order="__count DESC", limit=10)
        top_wo = Line._read_group(wl_dom + [("tx_type", "in", ("issue_site", "issue"))], ["work_order_id"], ["__count"],
                                  order="__count DESC", limit=10)
        cat = Balance._read_group(bdom + [("role", "=", "warehouse")], ["categ_id"], ["value:sum"], order="value:sum DESC", limit=10)
        charts = {
            "monthly": {"labels": mk, "unit": uom if single else "عدد البنود",
                        "series": [{"label": lbl, "data": [round(months[m][k], 3) for m in mk]} for k, lbl in
                                   (("rec", "مستلم"), ("iss", "مسلّم للمواقع"), ("con", "منفذ"), ("ret", "راجع"))]},
            "stages": {"labels": [stage_lbl.get(k, "غير محدد") for k in stage_counts], "data": list(stage_counts.values())},
            "top_materials": {"labels": [p.alw_sec_code or p.display_name for p, _c in top_mat], "data": [c for _p, c in top_mat]},
            "top_wos": {"labels": [w.name for w, _c in top_wo], "data": [c for _w, c in top_wo]},
            "categories": {"labels": [c.name or "—" for c, _v in cat], "data": [round(v or 0, 2) for _c, v in cat]},
        }
        prod = Product.browse(f["product_id"]) if single else Product
        return {"filters": {"date_from": str(f["date_from"]), "date_to": str(f["date_to"]),
                            "product": prod.display_name if prod else "", "product_id": prod.id or False,
                            "work_order": WO.browse(f["work_order_id"]).name if f["work_order_id"] and f["work_order_id"] > 0 else "",
                            "work_order_id": f["work_order_id"] if f["work_order_id"] and f["work_order_id"] > 0 else False,
                            "not_found": (f["product_id"] == -1 and "كود SEC غير موجود") or (f["work_order_id"] == -1 and "رقم أمر العمل غير موجود") or ""},
                "single": bool(single), "uom": uom, "kpis": kpis, "charts": charts,
                "materials": rows[:200], "materials_total": len(rows), "work_orders": wos,
                "wo_total": sum(stage_counts.values())}

    @api.model
    def export(self, code, filters, fmt):
        """Generate Excel/PDF through the report engine and return download URLs."""
        f = self._f(filters)
        rf = {"date_from": f["date_from"], "date_to": f["date_to"], "product_id": f["product_id"] if f["product_id"] and f["product_id"] > 0 else False,
              "work_order_id": f["work_order_id"] if f["work_order_id"] and f["work_order_id"] > 0 else False,
              "categ_id": f["categ_id"], "site_id": f["site_id"], "stage": f.get("stage") or False}
        hist = self.env["alwabrn.report.history"].export_and_log(code, rf, fmt)
        return ["/web/content/%s?download=true" % a.id for a in hist.attachment_ids]
