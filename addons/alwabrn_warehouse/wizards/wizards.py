import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..models.import_batch import CONTENT_TYPES
from ..models.report_engine import REPORTS
from ..models.common import riyadh_today
from ..tools import catalog_reader, xlsx_utils


class AlwabrnPasteWizard(models.TransientModel):
    _name = "alwabrn.paste.wizard"
    _description = "لصق البيانات"

    channel = fields.Selection([("paste", "لصق بيانات"), ("whatsapp", "رسالة واتساب (يدوي)"),
                                ("email", "نص بريد (يدوي)")], "المصدر", default="paste", required=True)
    content_type = fields.Selection(CONTENT_TYPES, "نوع البيانات", default="unknown", required=True,
                                    help="إن تُرك «غير مصنف» سيطلب النظام التصنيف قبل الاعتماد.")
    effective_date = fields.Date("تاريخ البيانات", default=lambda s: riyadh_today(), required=True)
    site_id = fields.Many2one("alwabrn.site", "الموقع (اختياري)")
    document_ref = fields.Char("رقم المستند")
    text = fields.Text("الصق البيانات هنا", required=True)

    def action_process(self):
        self.ensure_one()
        batch = self.env["alwabrn.import.batch"].create({
            "channel": self.channel, "content_type": self.content_type, "effective_date": self.effective_date,
            "site_id": self.site_id.id, "document_ref": self.document_ref, "subject": "لصق بيانات"})
        n = batch.load_text(self.text)
        if not n:
            raise UserError("لم يتم التعرف على أي سطر به كود أو كمية.")
        batch.action_validate()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.import.batch", "res_id": batch.id,
                "view_mode": "form"}


class AlwabrnUploadWizard(models.TransientModel):
    _name = "alwabrn.upload.wizard"
    _description = "رفع ملف / صورة"

    channel = fields.Selection([("excel", "ملف Excel / CSV"), ("image", "صورة"), ("camera", "كاميرا الجوال"),
                                ("pdf", "ملف PDF"), ("whatsapp", "مرفق واتساب"), ("email", "مرفق بريد")],
                               "المصدر", default="excel", required=True)
    content_type = fields.Selection(CONTENT_TYPES, "نوع البيانات", default="unknown", required=True)
    register_type = fields.Selection(lambda s: s.env["alwabrn.source.document"]._fields["register_type"].selection,
                                     string="نوع الدفتر", default="other")
    effective_date = fields.Date("تاريخ البيانات", default=lambda s: riyadh_today(), required=True)
    site_id = fields.Many2one("alwabrn.site", "الموقع (اختياري)")
    document_ref = fields.Char("رقم المستند")
    file = fields.Binary("الملف", required=True)
    filename = fields.Char("اسم الملف")
    file2 = fields.Binary("ملف إضافي")
    filename2 = fields.Char()
    file3 = fields.Binary("ملف إضافي 2")
    filename3 = fields.Char()
    manual_mapping = fields.Boolean("تحديد أعمدة Excel يدوياً")
    header_row = fields.Integer("رقم صف العناوين", default=1)
    col_sec = fields.Integer("عمود كود SEC", default=1)
    col_qty = fields.Integer("عمود الكمية", default=2)
    col_unit = fields.Integer("عمود الوحدة (0 = لا يوجد)")
    col_date = fields.Integer("عمود التاريخ (0 = لا يوجد)")
    col_site = fields.Integer("عمود الموقع (0 = لا يوجد)")
    detected = fields.Text("الأعمدة المكتشفة آلياً", readonly=True)

    @api.onchange("file", "filename")
    def _onchange_file(self):
        self.detected = False
        name = (self.filename or "").lower()
        if self.file and name.endswith((".xlsx", ".xls", ".csv")):
            try:
                rows = xlsx_utils.read_table(base64.b64decode(self.file), name)
                hdr, mapping = xlsx_utils.detect_columns(rows)
                self.header_row = hdr + 1
                self.col_sec = mapping.get("sec_code", -1) + 1
                self.col_qty = mapping.get("quantity", -1) + 1
                self.col_unit = mapping.get("unit", -1) + 1
                self.col_date = mapping.get("date", -1) + 1
                self.col_site = mapping.get("site", -1) + 1
                self.detected = "صف العناوين: %s | كود SEC: عمود %s | الكمية: عمود %s | الوحدة: %s | التاريخ: %s | الموقع: %s" % (
                    self.header_row, self.col_sec or "؟", self.col_qty or "؟", self.col_unit or "—",
                    self.col_date or "—", self.col_site or "—")
            except Exception as e:
                self.detected = "تعذر قراءة الملف: %s" % e

    def action_process(self):
        self.ensure_one()
        batch = self.env["alwabrn.import.batch"].create({
            "channel": self.channel, "content_type": self.content_type, "effective_date": self.effective_date,
            "site_id": self.site_id.id, "document_ref": self.document_ref, "subject": self.filename or "رفع ملف"})
        for data, fname in ((self.file, self.filename), (self.file2, self.filename2), (self.file3, self.filename3)):
            if not data:
                continue
            doc = self.env["alwabrn.source.document"].create({
                "name": fname or "upload", "datas": data, "channel": self.channel,
                "register_type": self.register_type})
            low = (fname or "").lower()
            if self.manual_mapping and low.endswith((".xlsx", ".xls", ".csv")):
                mapping = {k: v - 1 for k, v in (("sec_code", self.col_sec), ("quantity", self.col_qty),
                                                 ("unit", self.col_unit), ("date", self.col_date),
                                                 ("site", self.col_site)) if v}
                doc.batch_id = batch
                batch.load_table(doc._raw(), fname, mapping=mapping, header_row=self.header_row - 1, document=doc)
            else:
                batch.load_document(doc)
        batch.action_validate()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.import.batch", "res_id": batch.id,
                "view_mode": "form"}


class AlwabrnCatalogWizard(models.TransientModel):
    _name = "alwabrn.catalog.wizard"
    _description = "استيراد كتالوج المواد"

    file = fields.Binary("ملف المواد الموحدة (xlsx)", help="اتركه فارغاً لاستخدام الملف المرفق بالنظام.")
    filename = fields.Char()
    state = fields.Selection([("upload", "رفع"), ("preview", "معاينة"), ("done", "تم")], default="upload")
    line_ids = fields.One2many("alwabrn.catalog.wizard.line", "wizard_id", "المعاينة")
    summary = fields.Text("ملخص", readonly=True)
    report_file = fields.Binary("تقرير الأخطاء", readonly=True)
    report_filename = fields.Char()

    def _content(self):
        if self.file:
            return base64.b64decode(self.file)
        from odoo.tools.misc import file_open
        try:
            with file_open("alwabrn_warehouse/data/materials_2021.xlsx", "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise UserError("ارفع ملف «المواد الموحدة 2021.xlsx» (غير مرفق بالنسخة العامة لاحتوائه على أسعار العقد).")

    def action_preview(self):
        self.ensure_one()
        result = catalog_reader.read_catalog(self._content())
        Product = self.env["product.template"]
        self.line_ids.unlink()
        vals = []
        for rec in result["records"]:
            exists = bool(rec["sec_code"]) and bool(Product.search_count([("alw_sec_code", "=", rec["sec_code"])]))
            sev = "error" if any(i[0] == "error" for i in rec["issues"]) else "warning" if rec["issues"] else "ok"
            vals.append({"wizard_id": self.id, "row": rec["row"], "sec_code": rec["sec_code"],
                         "description": rec["description"], "unit": rec["unit"], "price": rec["price"],
                         "category": rec["category"] or "(تحتاج تحقق)", "severity": sev,
                         "issues": "؛ ".join(i[2] for i in rec["issues"]),
                         "action": "skip" if exists or sev == "error" else "create",
                         "exists": exists})
        self.env["alwabrn.catalog.wizard.line"].create(vals)
        refs = "\n".join("ورقة %s: %s — %s" % (r["sheet"], r["values"],
                                               "موجود بالورقة الرئيسية ولن يستورد مرتين" if r["already_in_main"]
                                               else "غير موجود بالورقة الرئيسية — للمراجعة") for r in result["reference_rows"])
        self.summary = "الورقة: %s | السجلات: %s | الفئات: %s | تحذيرات: %s | أخطاء: %s | موجود مسبقاً: %s\n%s" % (
            result["sheet"], len(vals), len(result["categories"]),
            len([v for v in vals if v["severity"] == "warning"]), len([v for v in vals if v["severity"] == "error"]),
            len([v for v in vals if v["exists"]]), refs)
        self.state = "preview"
        return self._reopen()

    def action_import(self):
        self.ensure_one()
        self.env["alwabrn.stock.service"].require_manager()
        from ..hooks import import_catalog
        stats = import_catalog(self.env, self._content())
        self.summary = (self.summary or "") + "\n\nتم الاستيراد: أُنشئ %(created)s | موجود مسبقاً %(existing)s | " \
            "يحتاج تحقق %(flagged)s | مرفوض %(rejected)s | فئات %(categories)s" % stats
        self.state = "done"
        self.env["alwabrn.approval"].log(self.env.user, "import", "استيراد كتالوج المواد", stats)
        return self._reopen()

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}


class AlwabrnCatalogWizardLine(models.TransientModel):
    _name = "alwabrn.catalog.wizard.line"
    _description = "سطر معاينة الكتالوج"
    _order = "severity desc, row"

    wizard_id = fields.Many2one("alwabrn.catalog.wizard", ondelete="cascade")
    row = fields.Integer("الصف")
    sec_code = fields.Char("كود SEC")
    description = fields.Char("الوصف")
    unit = fields.Char("الوحدة")
    price = fields.Float("السعر")
    category = fields.Char("الفئة")
    severity = fields.Selection([("ok", "سليم"), ("warning", "تحذير"), ("error", "خطأ")], "الحالة")
    issues = fields.Char("الملاحظات")
    exists = fields.Boolean("موجود")
    action = fields.Selection([("create", "إنشاء"), ("skip", "تخطي")], "الإجراء")


class AlwabrnReportWizard(models.TransientModel):
    _name = "alwabrn.report.wizard"
    _description = "تصدير وإرسال التقرير"

    report_code = fields.Selection(REPORTS, "التقرير", required=True, default="current_stock")
    date_from = fields.Date("من تاريخ", required=True, default=lambda s: riyadh_today())
    date_to = fields.Date("إلى تاريخ", required=True, default=lambda s: riyadh_today())
    location_id = fields.Many2one("stock.location", "المستودع / الموقع المخزني", domain=[("usage", "=", "internal")])
    site_id = fields.Many2one("alwabrn.site", "الموقع")
    project_id = fields.Many2one("alwabrn.project", "المشروع")
    work_order = fields.Char("أمر العمل")
    categ_id = fields.Many2one("product.category", "فئة المواد")
    product_id = fields.Many2one("product.product", "المادة", domain=[("alw_is_material", "=", True)])
    file_format = fields.Selection([("xlsx", "Excel"), ("pdf", "PDF"), ("both", "Excel + PDF")],
                                   "الصيغة", default="xlsx", required=True)
    recipient_ids = fields.Many2many("res.partner", string="المستلمون")
    message = fields.Text("رسالة مرفقة")
    preview_html = fields.Html("معاينة", readonly=True, sanitize=False)
    row_count = fields.Integer("عدد الصفوف", readonly=True)

    def _filters(self):
        return {"date_from": self.date_from, "date_to": self.date_to, "location_id": self.location_id.id,
                "site_id": self.site_id.id, "project_id": self.project_id.id, "work_order": self.work_order,
                "categ_id": self.categ_id.id, "product_id": self.product_id.id}

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}

    def action_preview(self):
        from markupsafe import escape, Markup
        self.ensure_one()
        Engine = self.env["alwabrn.report.engine"]
        report = Engine.get_report(self.report_code, self._filters())
        ser = Engine._serialize(report)
        html = Markup("<div dir='rtl'><h4>%s</h4><p>%s | %s</p><ul>%s</ul>") % (
            ser["title"], ser["period"], ser["filters"],
            Markup("").join(Markup("<li>%s: <b>%s</b></li>") % (k, v) for k, v in ser["summary"]))
        html += Markup("<table class='table table-sm table-bordered'><thead><tr>%s</tr></thead><tbody>%s</tbody></table>") % (
            Markup("").join(Markup("<th>%s</th>") % h for h in ser["headers"]),
            Markup("").join(Markup("<tr>%s</tr>") % Markup("").join(Markup("<td>%s</td>") % escape(c) for c in row)
                            for row in ser["rows"][:50]))
        if len(ser["rows"]) > 50:
            html += Markup("<p>… تعرض المعاينة أول 50 صفاً من %s.</p>") % len(ser["rows"])
        html += Markup("</div>")
        self.write({"preview_html": html, "row_count": len(ser["rows"])})
        return self._reopen()

    def action_export(self):
        self.ensure_one()
        hist = self.env["alwabrn.report.history"].export_and_log(self.report_code, self._filters(), self.file_format)
        if len(hist.attachment_ids) == 1:
            return {"type": "ir.actions.act_url", "target": "self",
                    "url": "/web/content/%s?download=true" % hist.attachment_ids.id}
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.report.history", "res_id": hist.id,
                "view_mode": "form"}

    def action_send(self):
        self.ensure_one()
        self.env["alwabrn.stock.service"].require_manager()
        emails = [e for e in self.recipient_ids.mapped("email") if e]
        if not emails:
            raise UserError("اختر مستلمين لديهم بريد إلكتروني.")
        hist = self.env["alwabrn.report.history"].export_and_log(
            self.report_code, self._filters(), self.file_format, action="email",
            recipients=emails, message=self.message)
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.report.history", "res_id": hist.id,
                "view_mode": "form"}


class AlwabrnSecretWizard(models.TransientModel):
    _name = "alwabrn.secret.wizard"
    _description = "إدخال بيانات اعتماد"

    account_id = fields.Many2one("alwabrn.integration.account", required=True)
    kind = fields.Selection(related="account_id.kind")
    access_token = fields.Char("Access Token")
    app_secret = fields.Char("App Secret")
    verify_token = fields.Char("Webhook Verify Token")
    api_key = fields.Char("API Key")

    def action_save(self):
        self.ensure_one()
        for key in ("access_token", "app_secret", "verify_token", "api_key"):
            if self[key]:
                self.account_id.set_secret(key, self[key])
        if self.kind == "whatsapp":
            self.account_id.wa_webhook_verified = False
        self.write({"access_token": False, "app_secret": False, "verify_token": False, "api_key": False})
        return {"type": "ir.actions.act_window_close"}
