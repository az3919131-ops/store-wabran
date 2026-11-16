import io
from datetime import timedelta

import xlsxwriter

from odoo import api, fields, models

from ..tools.xlsx_utils import safe_cell
from .common import riyadh_day_end_utc, riyadh_day_start_utc, riyadh_today
from .transaction import TX_TYPES

COMPANY_AR = "شركة الوبران — العقد الموحد للشركة السعودية للكهرباء"
BRANCH_AR = "فرع نجران — المملكة العربية السعودية"
FOOTER = "Project manager  Eng  Ahmed Zahran"

REPORTS = [
    ("current_stock", "الرصيد الحالي"),
    ("opening", "الأرصدة الافتتاحية"),
    ("daily_closing", "الأرصدة الختامية اليومية"),
    ("daily_in", "الوارد اليومي"),
    ("daily_out", "المنصرف اليومي"),
    ("movements", "سجل حركة المواد"),
    ("custody", "عهد المواقع"),
    ("consumption", "استهلاك المواد"),
    ("returns", "مرتجعات المواد"),
    ("damaged", "المواد التالفة"),
    ("scrap", "السكراب"),
    ("daily_recon", "المطابقة اليومية"),
    ("discrepancies", "فروقات الجرد"),
    ("monthly_summary", "الملخص الشهري للمخزون"),
    ("unmatched", "سجلات غير مطابقة / غير معرّفة"),
    ("pending", "الاعتمادات المعلقة"),
    ("audit", "سجل التدقيق"),
    ("executive", "الملخص التنفيذي للإدارة"),
    ("work_order", "تقرير أمر العمل: المستلم والمصروف والمسلّم والمنفذ والراجع"),
]


class AlwabrnReportEngine(models.AbstractModel):
    """One engine for the 18 reports: returns {columns, rows, summary} from
    real Odoo records; renders XLSX. No figure is hard-coded."""
    _name = "alwabrn.report.engine"
    _description = "محرك التقارير"

    # ------------------------------------------------------------- helpers
    def _f(self, filters):
        f = dict(filters or {})
        today = riyadh_today()
        f["date_from"] = fields.Date.to_date(f.get("date_from")) or today
        f["date_to"] = fields.Date.to_date(f.get("date_to")) or today
        f["dt_from"] = riyadh_day_start_utc(f["date_from"])
        f["dt_to"] = riyadh_day_end_utc(f["date_to"])
        return f

    def _tx_domain(self, f, types=None):
        dom = [("state", "in", ("posted", "reversed")), ("date", ">=", f["dt_from"]), ("date", "<=", f["dt_to"])]
        if types:
            dom.append(("tx_type", "in", types))
        if f.get("site_id"):
            dom.append(("site_id", "=", f["site_id"]))
        if f.get("project_id"):
            dom.append(("transaction_id.project_id", "=", f["project_id"]))
        if f.get("work_order"):
            dom.append(("transaction_id.work_order", "=", f["work_order"]))
        if f.get("work_order_id"):
            dom.append(("transaction_id.work_order_id", "=", f["work_order_id"]))
        if f.get("categ_id"):
            dom.append(("product_id.categ_id", "child_of", f["categ_id"]))
        if f.get("product_id"):
            dom.append(("product_id", "=", f["product_id"]))
        return dom

    def _tx_rows(self, f, types=None):
        labels = dict(TX_TYPES)
        lines = self.env["alwabrn.transaction.line"].sudo().search(self._tx_domain(f, types), order="date, id")
        cols = [("date", "التاريخ", "datetime"), ("name", "رقم الحركة", "text"), ("type", "النوع", "text"),
                ("sec", "كود SEC", "text"), ("desc", "الوصف", "text"), ("qty", "الكمية", "qty"),
                ("uom", "الوحدة", "text"), ("site", "الموقع", "text"), ("project", "المشروع", "text"),
                ("wo", "أمر العمل", "text"), ("doc", "مرجع المستند", "text"), ("status", "الحالة", "text")]
        # (wo column shows the linked work order when there is one)
        rows = [{
            "date": fields.Datetime.context_timestamp(self.with_context(tz="Asia/Riyadh"), l.date).replace(tzinfo=None),
            "name": l.transaction_id.name, "type": labels.get(l.tx_type), "sec": l.sec_code,
            "desc": l.product_id.name, "qty": l.qty_product_uom, "uom": l.product_id.uom_id.name,
            "site": l.site_id.display_name or "", "project": l.transaction_id.project_id.name or "",
            "wo": l.transaction_id.work_order_id.name or l.transaction_id.work_order or "", "doc": l.transaction_id.document_ref or "",
            "status": "معكوسة" if l.state == "reversed" else "مرحّلة"} for l in lines]
        by_unit = {}
        for r in rows:
            by_unit[r["uom"]] = by_unit.get(r["uom"], 0.0) + r["qty"]
        summary = [("عدد البنود", len(rows)), ("عدد المواد", len({r["sec"] for r in rows}))]
        summary += [("إجمالي الكمية (%s)" % u, q) for u, q in sorted(by_unit.items())]
        return cols, rows, summary

    def _balance_rows(self, f, role=None, at=None):
        svc = self.env["alwabrn.stock.service"]
        cols = [("sec", "كود SEC", "text"), ("desc", "الوصف", "text"), ("categ", "الفئة", "text"),
                ("uom", "الوحدة", "text"), ("qty", "الكمية", "qty"), ("price", "السعر المرجعي", "money"),
                ("value", "القيمة المرجعية", "money")]
        location = self.env["stock.location"].browse(f.get("location_id")) if f.get("location_id") \
            else svc.main_stock_location()
        data = svc.balance_at(location, at)
        products = self.env["product.product"].sudo().browse([p for p, q in data.items() if abs(q) > 1e-9])
        if f.get("categ_id"):
            cat = self.env["product.category"].browse(f["categ_id"])
            products = products.filtered(lambda p: p.categ_id.parent_path.startswith(cat.parent_path))
        if f.get("product_id"):
            products = products.filtered(lambda p: p.id == f["product_id"])
        rows = [{"sec": p.alw_sec_code or p.default_code or "", "desc": p.name, "categ": p.categ_id.name,
                 "uom": p.uom_id.name, "qty": data[p.id], "price": p.alw_reference_price,
                 "value": data[p.id] * p.alw_reference_price}
                for p in products.sorted(lambda p: p.alw_sec_code or "")]
        summary = [("الموقع المخزني", location.display_name), ("عدد المواد ذات الرصيد", len(rows)),
                   ("القيمة المرجعية الإجمالية (ر.س)", sum(r["value"] for r in rows))]
        return cols, rows, summary

    # ------------------------------------------------------------- reports
    def _r_current_stock(self, f):
        return self._balance_rows(f)

    def _r_daily_closing(self, f):
        cols, rows, summary = self._balance_rows(f, at=f["dt_to"])
        return cols, rows, [("الرصيد الختامي حتى نهاية يوم", str(f["date_to"]))] + summary

    def _r_opening(self, f):
        f = dict(f, dt_from=riyadh_day_start_utc(fields.Date.to_date("2000-01-01")))
        return self._tx_rows(f, ["opening"])

    def _r_daily_in(self, f):
        return self._tx_rows(f, ["receipt", "return_in"])

    def _r_daily_out(self, f):
        return self._tx_rows(f, ["issue_site", "issue", "return_out"])

    def _r_movements(self, f):
        return self._tx_rows(f)

    def _r_consumption(self, f):
        return self._tx_rows(f, ["consume", "issue"])

    def _r_returns(self, f):
        return self._tx_rows(f, ["return_in", "return_out"])

    def _r_damaged(self, f):
        return self._tx_rows(f, ["damaged"])

    def _r_scrap(self, f):
        return self._tx_rows(f, ["scrap"])

    def _r_custody(self, f):
        dom = []
        for key in ("site_id", "project_id", "product_id"):
            if f.get(key):
                dom.append((key, "=", f[key]))
        if f.get("work_order"):
            dom.append(("work_order", "=", f["work_order"]))
        lines = self.env["alwabrn.custody.line"].sudo().search(dom)
        cols = [("site", "الموقع", "text"), ("project", "المشروع", "text"), ("wo", "أمر العمل", "text"),
                ("supervisor", "المشرف", "text"), ("sec", "كود SEC", "text"), ("desc", "الوصف", "text"),
                ("uom", "الوحدة", "text"), ("issued", "المصروف", "qty"), ("returned", "المرتجع", "qty"),
                ("consumed", "المستهلك", "qty"), ("other", "تالف/أخرى", "qty"), ("out", "العهدة القائمة", "qty")]
        rows = [{"site": l.site_id.display_name, "project": l.project_id.name or "", "wo": l.work_order or "",
                 "supervisor": l.supervisor_id.name or "", "sec": l.sec_code, "desc": l.product_id.name,
                 "uom": l.uom_id.name, "issued": l.qty_issued, "returned": l.qty_returned,
                 "consumed": l.qty_consumed, "other": l.qty_other_out, "out": l.qty_outstanding} for l in lines]
        summary = [("عدد المواقع", len({r["site"] for r in rows})),
                   ("عدد البنود ذات عهدة قائمة", len([r for r in rows if r["out"] > 0]))]
        return cols, rows, summary

    def _recon_rows(self, f, only_diff):
        dom = [("date", ">=", f["date_from"]), ("date", "<=", f["date_to"])]
        if only_diff:
            dom.append(("status", "=", "diff"))
        if f.get("product_id"):
            dom.append(("product_id", "=", f["product_id"]))
        if f.get("recon_status"):
            dom.append(("status", "=", f["recon_status"]))
        lines = self.env["alwabrn.reconciliation.line"].sudo().search(dom, order="date, sec_code")
        st = dict(lines._fields["status"]._description_selection(self.env))
        cl = dict(lines._fields["classification"]._description_selection(self.env))
        rs = dict(lines._fields["resolution"]._description_selection(self.env))
        cols = [("date", "التاريخ", "date"), ("ref", "المطابقة", "text"), ("sec", "كود SEC", "text"),
                ("desc", "الوصف", "text"), ("uom", "الوحدة", "text"), ("odoo", "رصيد Odoo", "qty"),
                ("book", "الدفتري", "qty"), ("phys", "الفعلي", "qty"), ("diff", "الفرق", "qty"),
                ("status", "الحالة", "text"), ("cls", "التصنيف", "text"), ("res", "المعالجة", "text"),
                ("note", "ملاحظات", "text")]
        rows = [{"date": l.date, "ref": l.reconciliation_id.name, "sec": l.sec_code, "desc": l.product_id.name,
                 "uom": l.uom_id.name, "odoo": l.odoo_qty, "book": l.book_qty if l.has_book else None,
                 "phys": l.physical_qty if l.has_physical else None, "diff": l.difference,
                 "status": st.get(l.status), "cls": cl.get(l.classification) or "", "res": rs.get(l.resolution),
                 "note": l.note or l.analysis or ""} for l in lines]
        summary = [("عدد البنود", len(rows)), ("مطابق", len(lines.filtered(lambda l: l.status == "match"))),
                   ("فروقات", len(lines.filtered(lambda l: l.status == "diff"))),
                   ("ملاحظة", "الخانة الفارغة = بيانات غير متوفرة (لا تعامل كصفر)")]
        return cols, rows, summary

    def _r_daily_recon(self, f):
        return self._recon_rows(f, False)

    def _r_discrepancies(self, f):
        return self._recon_rows(f, True)

    def _r_monthly_summary(self, f):
        svc = self.env["alwabrn.stock.service"]
        wh = svc.main_stock_location()
        opening = svc.balance_at(wh, f["dt_from"] - timedelta(seconds=1))
        closing = svc.balance_at(wh, f["dt_to"])
        lines = self.env["alwabrn.transaction.line"].sudo().search(self._tx_domain(f))
        agg = {}
        for l in lines.filtered(lambda l: l.state == "posted" or True):
            d = agg.setdefault(l.product_id.id, {})
            d[l.tx_type] = d.get(l.tx_type, 0.0) + l.qty_product_uom
        pids = {p for p, q in opening.items() if abs(q) > 1e-9} | {p for p, q in closing.items() if abs(q) > 1e-9} | set(agg)
        cols = [("sec", "كود SEC", "text"), ("desc", "الوصف", "text"), ("uom", "الوحدة", "text"),
                ("open", "رصيد أول المدة", "qty"), ("in", "الوارد", "qty"), ("ret", "مرتجع من المواقع", "qty"),
                ("iss", "منصرف للمواقع", "qty"), ("dir", "صرف مباشر", "qty"), ("cons", "استهلاك بالمواقع", "qty"),
                ("oth", "تالف/سكراب/إرجاع", "qty"), ("adj", "تسويات (صافي)", "qty"), ("close", "رصيد آخر المدة", "qty")]
        rows = []
        for p in self.env["product.product"].sudo().browse(list(pids)).sorted(lambda p: p.alw_sec_code or ""):
            a = agg.get(p.id, {})
            rows.append({"sec": p.alw_sec_code or "", "desc": p.name, "uom": p.uom_id.name,
                         "open": opening.get(p.id, 0.0), "in": a.get("receipt", 0.0), "ret": a.get("return_in", 0.0),
                         "iss": a.get("issue_site", 0.0), "dir": a.get("issue", 0.0), "cons": a.get("consume", 0.0),
                         "oth": a.get("damaged", 0.0) + a.get("scrap", 0.0) + a.get("return_out", 0.0),
                         "adj": a.get("adj_plus", 0.0) + a.get("opening", 0.0) - a.get("adj_minus", 0.0),
                         "close": closing.get(p.id, 0.0)})
        return cols, rows, [("عدد المواد", len(rows)),
                            ("ملاحظة", "أرصدة أول/آخر المدة للمستودع الرئيسي محسوبة من حركات Odoo المرحّلة")]

    def _r_unmatched(self, f):
        lines = self.env["alwabrn.import.line"].sudo().search([
            ("status", "=", "error"), ("excluded", "=", False),
            ("batch_id.state", "not in", ("approved", "posted", "rejected")),
            ("create_date", ">=", f["dt_from"]), ("create_date", "<=", f["dt_to"])])
        cols = [("batch", "الدفعة", "text"), ("no", "السطر", "int"), ("raw", "النص الأصلي", "text"),
                ("sec", "كود SEC المقروء", "text"), ("qty", "الكمية", "qty"), ("issue", "المشكلة", "text")]
        rows = [{"batch": l.batch_id.name, "no": l.line_no, "raw": l.raw_text or "", "sec": l.sec_code or "",
                 "qty": l.quantity, "issue": l.issue_text or ""} for l in lines]
        return cols, rows, [("عدد السجلات غير المطابقة", len(rows))]

    def _r_pending(self, f):
        labels = dict(TX_TYPES)
        cols = [("kind", "النوع", "text"), ("ref", "المرجع", "text"), ("what", "البيان", "text"),
                ("by", "أدخل بواسطة", "text"), ("date", "تاريخ الإنشاء", "datetime"), ("status", "الحالة", "text")]
        rows = []
        for t in self.env["alwabrn.transaction"].sudo().search([("state", "in", ("draft", "pending"))]):
            rows.append({"kind": "حركة مواد", "ref": t.name, "what": labels.get(t.tx_type),
                         "by": t.employee_id.name, "date": t.create_date,
                         "status": "بانتظار الاعتماد" if t.state == "pending" else "مسودة"})
        for b in self.env["alwabrn.import.batch"].sudo().search(
                [("state", "in", ("draft", "needs_review", "validated"))]):
            rows.append({"kind": "دفعة إدخال", "ref": b.name, "what": b.subject or "", "by": b.submitted_by.name,
                         "date": b.create_date, "status": dict(b._fields["state"]._description_selection(self.env))[b.state]})
        for s in self.env["alwabrn.daily.snapshot"].sudo().search([("state", "=", "draft")]):
            rows.append({"kind": "كشف رصيد", "ref": s.name, "what": str(s.balance_date), "by": s.submitted_by.name,
                         "date": s.create_date, "status": "مسودة"})
        return cols, rows, [("إجمالي المعلق", len(rows))]

    def _r_audit(self, f):
        logs = self.env["alwabrn.approval"].sudo().search(
            [("date", ">=", f["dt_from"]), ("date", "<=", f["dt_to"])], order="date")
        act = dict(logs._fields["action"]._description_selection(self.env))
        cols = [("date", "التوقيت", "datetime"), ("user", "المستخدم", "text"), ("action", "الإجراء", "text"),
                ("ref", "المرجع", "text"), ("model", "النموذج", "text"), ("note", "ملاحظات", "text"),
                ("details", "تفاصيل", "text")]
        rows = [{"date": l.date, "user": l.user_id.name, "action": act.get(l.action), "ref": l.name,
                 "model": l.res_model, "note": l.note or "", "details": l.details or ""} for l in logs]
        return cols, rows, [("عدد السجلات", len(rows))]

    def _r_work_order(self, f):
        """One row per work order × material: received from SEC, ISSUE (minutes), delivered to site,
        installed, returned to warehouse, returned to SEC and balance at site. All dates (no period cut)."""
        WO = self.env["alwabrn.work.order"].sudo()
        dom = [("id", "=", f["work_order_id"])] if f.get("work_order_id") else [("material_ids", "!=", False)]
        cols = [("wo", "أمر العمل", "text"), ("type", "نوع أمر العمل", "text"), ("sec", "كود SEC", "text"),
                ("desc", "الوصف", "text"), ("uom", "الوحدة", "text"),
                ("rec", "مستلم من الكهرباء", "qty"), ("iss_sec", "مصروف من الكهرباء ISSUE (محضر)", "qty"),
                ("iss", "مسلّم للموقع", "qty"), ("erc", "المنفذ", "qty"), ("ret", "راجع للمستودع", "qty"),
                ("ret_sec", "مرتجع للكهرباء", "qty"), ("ret_min", "المرتجع حسب المحضر", "qty"),
                ("net", "باقي بالموقع", "qty"), ("diff", "فرق التسليم عن ISSUE", "qty")]
        rows = []
        orders = WO.search(dom, order="name")
        for wo in orders:
            for m in wo.material_ids.sorted("sec_code"):
                rows.append({"wo": wo.name, "type": wo.type_summary or "", "sec": m.sec_code, "desc": m.name or "",
                             "uom": m.uom_id.name or m.unit_text or "", "rec": m.wh_received, "iss_sec": m.sec_issued,
                             "iss": m.wh_issued, "erc": m.erected, "ret": m.wh_returned, "ret_sec": m.wh_returned_sec,
                             "ret_min": m.sec_returned, "net": m.wh_net, "diff": m.diff_issue})
        summary = [("عدد أوامر العمل", len(orders)), ("عدد بنود المواد", len(rows))]
        if f.get("work_order_id") and orders:
            wo = orders[0]
            summary += [("أمر العمل", wo.name), ("النوع", wo.type_summary or "—"), ("المرحلة", dict(
                wo._fields["stage"]._description_selection(self.env)).get(wo.stage) or "—"),
                ("المقاول", wo.contractor or "—"), ("الموقع", wo.location or "—"), ("الموقف", wo.status_note or "—"),
                ("محاضر الاستلام", wo.minutes_count), ("حركات المستودع", wo.tx_count)]
        summary.append(("ملاحظة", "المنفذ = حسب المحضر أو الاستهلاك المسجل أيهما أكبر · الباقي بالموقع = المسلّم − الراجع − المنفذ"))
        return cols, rows, summary

    def _r_executive(self, f):
        data = self.env["alwabrn.dashboard"].get_data({"date_from": str(f["date_from"]), "date_to": str(f["date_to"])})
        cols = [("kpi", "المؤشر", "text"), ("value", "القيمة", "text")]
        rows = [{"kpi": k["label"], "value": k["display"]} for k in data["kpis"]]
        return cols, rows, [("الفترة", "%s — %s" % (f["date_from"], f["date_to"]))]

    # ---------------------------------------------------------------- API
    @api.model
    def get_report(self, code, filters=None):
        f = self._f(filters)
        cols, rows, summary = getattr(self, "_r_%s" % code)(f)
        return {"code": code, "title": dict(REPORTS)[code], "columns": cols, "rows": rows, "summary": summary,
                "date_from": f["date_from"], "date_to": f["date_to"], "filters": self._filters_text(f)}

    def _filters_text(self, f):
        parts = []
        for key, model, label in (("site_id", "alwabrn.site", "الموقع"), ("project_id", "alwabrn.project", "المشروع"),
                                  ("categ_id", "product.category", "الفئة"), ("product_id", "product.product", "المادة"),
                                  ("location_id", "stock.location", "المستودع"),
                                  ("work_order_id", "alwabrn.work.order", "أمر العمل")):
            if f.get(key):
                parts.append("%s: %s" % (label, self.env[model].sudo().browse(f[key]).display_name))
        if f.get("work_order"):
            parts.append("أمر العمل: %s" % f["work_order"])
        return "؛ ".join(parts) or "بدون فلاتر"

    @api.model
    def render_xlsx(self, report):
        buf = io.BytesIO()
        # strings_to_formulas=False + safe_cell => no formula injection
        wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
        title = wb.add_format({"bold": True, "font_size": 14, "align": "center", "bg_color": "#0B5345", "font_color": "white"})
        sub = wb.add_format({"bold": True, "align": "center", "bg_color": "#D5F5E3"})
        head = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white", "border": 1,
                              "align": "center", "valign": "vcenter", "text_wrap": True})
        fmts = {
            "text": wb.add_format({"border": 1}), "qty": wb.add_format({"border": 1, "num_format": "#,##0.###"}),
            "money": wb.add_format({"border": 1, "num_format": "#,##0.00"}),
            "int": wb.add_format({"border": 1, "num_format": "0"}),
            "date": wb.add_format({"border": 1, "num_format": "yyyy-mm-dd"}),
            "datetime": wb.add_format({"border": 1, "num_format": "yyyy-mm-dd hh:mm"}),
        }
        label = wb.add_format({"bold": True, "border": 1, "bg_color": "#F2F2F2"})
        cols = report["columns"]
        meta = [("الشركة", COMPANY_AR), ("الفرع", BRANCH_AR), ("التقرير", report["title"]),
                ("الفترة", "%s — %s" % (report["date_from"], report["date_to"])),
                ("تاريخ التصدير", fields.Datetime.context_timestamp(
                    self.with_context(tz="Asia/Riyadh"), fields.Datetime.now()).strftime("%Y-%m-%d %H:%M")),
                ("الفلاتر", report["filters"])]
        # summary sheet
        ws = wb.add_worksheet("الملخص")
        ws.right_to_left()
        ws.set_column(0, 0, 38); ws.set_column(1, 1, 60)
        ws.merge_range(0, 0, 0, 1, COMPANY_AR, title)
        ws.merge_range(1, 0, 1, 1, report["title"], sub)
        r = 3
        for k, v in meta + list(report["summary"]):
            ws.write(r, 0, k, label)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                ws.write_number(r, 1, v, fmts["qty"])
            else:
                ws.write_string(r, 1, str(safe_cell(str(v))), fmts["text"])
            r += 1
        ws.write(r + 1, 0, FOOTER)
        # detail sheet
        wd = wb.add_worksheet("التفاصيل")
        wd.right_to_left()
        for c, (_k, lbl, _t) in enumerate(cols):
            wd.write(0, c, lbl, head)
        widths = [len(lbl) + 4 for _k, lbl, _t in cols]
        for i, row in enumerate(report["rows"], start=1):
            for c, (key, _lbl, typ) in enumerate(cols):
                v = row.get(key)
                if v is None or v is False or v == "":
                    wd.write_blank(i, c, None, fmts["text"])
                elif typ in ("qty", "money", "int") and isinstance(v, (int, float)):
                    wd.write_number(i, c, v, fmts[typ])
                elif typ in ("date", "datetime") and hasattr(v, "year"):
                    wd.write_datetime(i, c, v, fmts[typ])
                    widths[c] = max(widths[c], 17)
                else:
                    s = str(safe_cell(str(v)))
                    wd.write_string(i, c, s, fmts["text"])
                    widths[c] = max(widths[c], min(len(s) + 2, 60))
        for c, w in enumerate(widths):
            wd.set_column(c, c, w)
        wd.freeze_panes(1, 0)
        if cols:
            wd.autofilter(0, 0, max(len(report["rows"]), 1), len(cols) - 1)
        wd.write(len(report["rows"]) + 2, 0, FOOTER)
        wb.close()
        return buf.getvalue()

    @api.model
    def render_pdf(self, report):
        html_report = self.env.ref("alwabrn_warehouse.action_report_generic")
        data = {"report": self._serialize(report), "company": COMPANY_AR, "branch": BRANCH_AR, "footer": FOOTER,
                "exported": fields.Datetime.context_timestamp(
                    self.with_context(tz="Asia/Riyadh"), fields.Datetime.now()).strftime("%Y-%m-%d %H:%M")}
        pdf, _fmt = self.env["ir.actions.report"].sudo()._render_qweb_pdf(html_report, [0], data=data)
        return pdf

    def _serialize(self, report):
        def fmt(v, typ):
            if v is None or v is False:
                return ""
            if typ == "qty" and isinstance(v, (int, float)):
                return ("%.3f" % v).rstrip("0").rstrip(".")
            if typ == "money" and isinstance(v, (int, float)):
                return "{:,.2f}".format(v)
            if typ == "datetime" and hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d %H:%M")
            return str(v)
        cols = report["columns"]
        return {"title": report["title"], "filters": report["filters"],
                "period": "%s — %s" % (report["date_from"], report["date_to"]),
                "headers": [c[1] for c in cols],
                "rows": [[fmt(row.get(k), t) for k, _l, t in cols] for row in report["rows"]],
                "summary": [(k, fmt(v, "qty")) for k, v in report["summary"]]}
