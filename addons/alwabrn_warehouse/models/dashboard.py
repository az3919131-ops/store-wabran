from datetime import timedelta

from odoo import api, fields, models

from .common import riyadh_day_end_utc, riyadh_day_start_utc, riyadh_today

IN_TYPES = ("receipt", "return_in")
OUT_TYPES = ("issue_site", "issue", "return_out")


class AlwabrnDashboard(models.AbstractModel):
    _name = "alwabrn.dashboard"
    _description = "لوحة المؤشرات"

    @api.model
    def get_data(self, filters=None):
        """All figures are computed live from Odoo records. Quantities with
        different units are never summed together: we report counts, money,
        or per-unit breakdowns."""
        f = filters or {}
        today = riyadh_today()
        d_from = fields.Date.to_date(f.get("date_from")) or today - timedelta(days=29)
        d_to = fields.Date.to_date(f.get("date_to")) or today
        dt_from, dt_to = riyadh_day_start_utc(d_from), riyadh_day_end_utc(d_to)
        t_from, t_to = riyadh_day_start_utc(today), riyadh_day_end_utc(today)
        Tx = self.env["alwabrn.transaction"]
        TxLine = self.env["alwabrn.transaction.line"]
        Balance = self.env["alwabrn.stock.balance"]
        ReconLine = self.env["alwabrn.reconciliation.line"]
        Batch = self.env["alwabrn.import.batch"]
        Product = self.env["product.template"]

        line_dom = [("state", "=", "posted"), ("date", ">=", dt_from), ("date", "<=", dt_to)]
        bal_dom = []
        for key, lfield, bfield in (("site_id", "site_id", "site_id"),
                                    ("product_id", "product_id", "product_id")):
            if f.get(key):
                line_dom.append((lfield, "=", f[key]))
                bal_dom.append((bfield, "=", f[key]))
        if f.get("project_id"):
            line_dom.append(("transaction_id.project_id", "=", f["project_id"]))
        if f.get("work_order"):
            line_dom.append(("transaction_id.work_order", "=", f["work_order"]))
        if f.get("categ_id"):
            line_dom.append(("product_id.categ_id", "child_of", f["categ_id"]))
            bal_dom.append(("categ_id", "child_of", f["categ_id"]))
        if f.get("tx_type"):
            line_dom.append(("tx_type", "=", f["tx_type"]))

        def tx_count(types, a, b, state="posted"):
            return Tx.search_count([("tx_type", "in", types), ("state", "=", state),
                                    ("date", ">=", a), ("date", "<=", b)])

        wh = Balance._read_group(bal_dom + [("role", "=", "warehouse")], [],
                                 ["product_id:count_distinct", "value:sum"])[0]
        cust = Balance._read_group(bal_dom + [("role", "=", "custody")], [],
                                   ["product_id:count_distinct", "value:sum", "site_id:count_distinct"])[0]
        last_recon = self.env["alwabrn.reconciliation"].search([("state", "!=", "draft")], order="computed_on desc", limit=1)
        last_batch = Batch.search([], order="id desc", limit=1)
        latest_lines = ReconLine.search([("reconciliation_id", "=", last_recon.id)]) if last_recon else ReconLine
        open_diff = ReconLine.search_count([("status", "=", "diff"), ("resolution", "in", ("open", "adjust_pending"))])
        pending_docs = Batch.search_count([("state", "in", ("draft", "processing", "needs_review", "validated"))])
        pending_tx = Tx.search_count([("state", "in", ("draft", "pending"))])
        total_materials = Product.search_count([("alw_is_material", "=", True)])

        def kpi(key, label, value, display=None, action=None, tone="info"):
            return {"key": key, "label": label, "value": value,
                    "display": display if display is not None else "{:,}".format(value), "action": action, "tone": tone}

        money = lambda v: "{:,.0f} ر.س".format(v or 0.0)  # noqa: E731
        fmt_dt = lambda dt: fields.Datetime.context_timestamp(  # noqa: E731
            self.with_context(tz="Asia/Riyadh"), dt).strftime("%Y-%m-%d %H:%M") if dt else "لا يوجد"
        kpis = [
            kpi("materials", "إجمالي عدد المواد", total_materials, action="materials"),
            kpi("wh_items", "مواد لها رصيد بالمستودع", wh[0] or 0, action="balance"),
            kpi("wh_value", "قيمة رصيد المستودع (مرجعية)", wh[1] or 0.0, money(wh[1]), "balance"),
            kpi("in_today", "حركات الدخول اليوم", tx_count(IN_TYPES, t_from, t_to), action="in", tone="good"),
            kpi("out_today", "حركات الخروج اليوم", tx_count(OUT_TYPES, t_from, t_to), action="out", tone="warn"),
            kpi("custody_items", "مواد بعهدة المواقع", cust[0] or 0, action="custody"),
            kpi("custody_value", "إجمالي عهد المواقع (مرجعية)", cust[1] or 0.0, money(cust[1]), "custody"),
            kpi("custody_sites", "مواقع لديها عهدة قائمة", cust[2] or 0, action="custody"),
            kpi("returns", "حركات المرتجعات (الفترة)", tx_count(("return_in",), dt_from, dt_to), action="in"),
            kpi("damaged", "حركات مواد تالفة (الفترة)", tx_count(("damaged",), dt_from, dt_to)),
            kpi("scrap", "حركات سكراب (الفترة)", tx_count(("scrap",), dt_from, dt_to)),
            kpi("matched", "مواد تمت مطابقتها (آخر مطابقة)",
                len(latest_lines.filtered(lambda l: l.status == "match")), action="recon", tone="good"),
            kpi("discrepancies", "مواد بها فروقات مفتوحة", open_diff, action="diff",
                tone="bad" if open_diff else "good"),
            kpi("pending_docs", "مستندات قيد المراجعة", pending_docs, action="batches",
                tone="warn" if pending_docs else "good"),
            kpi("pending_tx", "حركات غير معتمدة", pending_tx, action="pending_tx",
                tone="warn" if pending_tx else "good"),
            kpi("work_orders", "أوامر العمل", self.env["alwabrn.work.order"].search_count([]), action="work_orders"),
            kpi("wo_minutes", "أوامر عمل لها محاضر استلام", self.env["alwabrn.work.order"].search_count([("minutes_ids", "!=", False)]),
                action="work_orders", tone="good"),
            kpi("wo_return", "أوامر عمل باقي ارجاع مواد", self.env["alwabrn.work.order"].search_count([("status_note", "ilike", "ارجاع")]),
                action="work_orders", tone="warn"),
            kpi("last_import", "آخر عملية استيراد", 0,
                "%s — %s" % (last_batch.name, fmt_dt(last_batch.create_date)) if last_batch else "لا يوجد", "batches"),
            kpi("last_recon", "آخر عملية مطابقة", 0,
                "%s — %s" % (last_recon.name, fmt_dt(last_recon.computed_on)) if last_recon else "لا يوجد", "recon"),
        ]

        # ---- charts (transaction counts / per-material quantities only)
        days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)][-62:]
        per_day = {d: {"in": 0, "out": 0} for d in days}
        for tx in Tx.search([("state", "=", "posted"), ("date", ">=", dt_from), ("date", "<=", dt_to),
                             ("tx_type", "in", IN_TYPES + OUT_TYPES)]):
            day = fields.Datetime.context_timestamp(self.with_context(tz="Asia/Riyadh"), tx.date).date()
            if day in per_day:
                per_day[day]["in" if tx.tx_type in IN_TYPES else "out"] += 1
        monthly = {}
        labels_tx = dict(Tx._fields["tx_type"]._description_selection(self.env))
        for tx_type, month, count in Tx._read_group(
                [("state", "=", "posted"), ("date", ">=", dt_to - timedelta(days=365))],
                ["tx_type", "date:month"], ["__count"]):
            monthly.setdefault(month.strftime("%Y-%m"), {})[tx_type] = count
        top = TxLine._read_group(line_dom, ["product_id"], ["__count", "qty_product_uom:sum"],
                                 order="__count DESC", limit=10)
        cat = Balance._read_group(bal_dom + [("role", "=", "warehouse")], ["categ_id"],
                                  ["value:sum", "product_id:count_distinct"], order="value:sum DESC", limit=12)
        site_cust = Balance._read_group(bal_dom + [("role", "=", "custody")], ["site_id"],
                                        ["value:sum", "product_id:count_distinct"], order="value:sum DESC", limit=12)
        cons = TxLine._read_group(line_dom + [("tx_type", "in", ("consume", "issue"))],
                                  ["site_id"], ["__count"], limit=12)
        trend = ReconLine._read_group([("date", ">=", d_from), ("date", "<=", d_to), ("status", "=", "diff")],
                                      ["date:day"], ["__count"], order="date:day")
        frequent = ReconLine._read_group([("status", "=", "diff")], ["product_id"], ["__count"],
                                         order="__count DESC", limit=10)
        charts = {
            "daily": {"labels": [d.strftime("%m-%d") for d in days],
                      "in": [per_day[d]["in"] for d in days], "out": [per_day[d]["out"] for d in days]},
            "monthly": {"labels": sorted(monthly),
                        "series": [{"label": labels_tx[t], "data": [monthly[m].get(t, 0) for m in sorted(monthly)]}
                                   for t in sorted({t for m in monthly.values() for t in m})]},
            "top": {"labels": [p.alw_sec_code or p.display_name for p, _c, _q in top],
                    "data": [c for _p, c, _q in top],
                    "names": ["%s — %s %s" % (p.name, round(q, 3), p.uom_id.name) for p, _c, q in top]},
            "categories": {"labels": [c.name or "غير مصنف" for c, _v, _n in cat],
                           "data": [round(v or 0.0, 2) for _c, v, _n in cat]},
            "site_custody": {"labels": [s.display_name or "—" for s, _v, _n in site_cust],
                             "data": [round(v or 0.0, 2) for _s, v, _n in site_cust],
                             "items": [n for _s, _v, n in site_cust]},
            "consumption": {"labels": [s.display_name or "صرف مباشر من المستودع" for s, _c in cons],
                            "data": [c for _s, c in cons]},
            "trend": {"labels": [d.strftime("%m-%d") for d, _c in trend], "data": [c for _d, c in trend]},
            "frequent": {"labels": [p.alw_sec_code or p.display_name for p, _c in frequent],
                         "data": [c for _p, c in frequent]},
        }
        user = self.env.user
        return {"kpis": kpis, "charts": charts, "date_from": str(d_from), "date_to": str(d_to),
                "is_manager": user.has_group("alwabrn_warehouse.group_alw_manager"),
                "can_write": user.has_group("alwabrn_warehouse.group_alw_employee"),
                "user_name": user.name}

    @api.model
    def get_filter_options(self):
        return {
            "sites": self.env["alwabrn.site"].search_read([], ["display_name"]),
            "projects": self.env["alwabrn.project"].search_read([], ["name"]),
            "categories": self.env["product.category"].search_read(
                [("parent_id", "=", self.env.ref("alwabrn_warehouse.categ_sec_materials").id)], ["name"]),
            "tx_types": self.env["alwabrn.transaction"]._fields["tx_type"]._description_selection(self.env),
        }
