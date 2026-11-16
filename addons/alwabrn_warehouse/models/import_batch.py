import base64
import hashlib
import io
import re

import xlsxwriter

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..tools import text_parser, xlsx_utils
from .common import riyadh_today
from .source_document import CHANNELS, LOW_CONFIDENCE

CONTENT_TYPES = [
    ("unknown", "غير مصنف (يجب التصنيف)"),
    ("opening", "رصيد افتتاحي"),
    ("receipt", "استلام مواد"),
    ("issue_site", "صرف إلى موقع"),
    ("issue", "صرف مباشر"),
    ("return_in", "ارتجاع من موقع"),
    ("consume", "استهلاك بالموقع"),
    ("book_balance", "رصيد دفتري يومي (لا يغير المخزون)"),
    ("physical_count", "جرد فعلي (لا يغير المخزون)"),
]
STOCK_TYPES = ("opening", "receipt", "issue_site", "issue", "return_in", "consume")
SNAPSHOT_TYPES = {"book_balance": "book", "physical_count": "physical"}
DETECTED_MAP = {"receipt": "receipt", "issue": "issue_site", "return_in": "return_in",
                "consume": "consume", "balance": "book_balance", "count": "physical_count"}
ISSUE_LABELS = {
    "no_sec_code": "لا يوجد كود SEC مقروء", "unknown_material": "كود SEC غير موجود بالكتالوج",
    "no_quantity": "الكمية غير مقروءة", "bad_quantity": "كمية غير صالحة",
    "multiple_numbers": "أكثر من رقم بالسطر — تأكد من الكمية", "multiple_codes": "أكثر من كود بالسطر",
    "bad_unit": "وحدة غير معروفة", "unit_incompatible": "الوحدة غير متوافقة مع وحدة المادة",
    "low_confidence": "ثقة OCR منخفضة — راجع القيمة", "dup_in_batch": "صف مكرر داخل الدفعة",
    "site_unknown": "الموقع غير معرف", "site_missing": "الموقع مطلوب لهذا النوع",
    "suspicious_qty": "كمية مشبوهة (كبيرة جداً)", "bad_date": "تاريخ غير صالح أو مستقبلي",
    "possible_duplicate": "حركة مشابهة مسجلة مسبقاً (تكرار محتمل)",
}
ERROR_CODES = {"no_sec_code", "unknown_material", "no_quantity", "bad_quantity", "bad_unit",
               "unit_incompatible", "site_unknown", "site_missing", "bad_date"}


class AlwabrnImportBatch(models.Model):
    _name = "alwabrn.import.batch"
    _description = "دفعة إدخال موحدة"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char("المرجع", readonly=True, copy=False, default="جديد", index=True)
    subject = fields.Char("العنوان")
    channel = fields.Selection(CHANNELS, "قناة الورود", required=True, default="manual", tracking=True, index=True)
    content_type = fields.Selection(CONTENT_TYPES, "نوع البيانات", required=True, default="unknown", tracking=True)
    state = fields.Selection([
        ("draft", "مسودة"), ("processing", "قيد المعالجة"), ("needs_review", "يحتاج مراجعة"),
        ("validated", "تم التحقق"), ("approved", "معتمدة"), ("posted", "مرحّلة للمخزون"),
        ("rejected", "مرفوضة"), ("failed", "فشلت"),
    ], "الحالة", default="draft", required=True, tracking=True, index=True, copy=False)
    effective_date = fields.Date("تاريخ البيانات", default=lambda s: riyadh_today(), required=True, tracking=True)
    site_id = fields.Many2one("alwabrn.site", "الموقع (افتراضي للأسطر)")
    location_id = fields.Many2one("stock.location", "المستودع", domain=[("usage", "=", "internal")],
                                  default=lambda s: s.env["alwabrn.stock.service"].main_stock_location())
    document_ref = fields.Char("رقم المستند")
    raw_text = fields.Text("النص الأصلي")
    line_ids = fields.One2many("alwabrn.import.line", "batch_id", "الأسطر")
    source_document_ids = fields.One2many("alwabrn.source.document", "batch_id", "المستندات المصدر")
    preview_image = fields.Binary("صورة المستند", compute="_compute_preview_image")
    submitted_by = fields.Many2one("res.users", "أدخل بواسطة", default=lambda s: s.env.user, readonly=True)
    approved_by = fields.Many2one("res.users", "اعتمد بواسطة", readonly=True, copy=False)
    approved_on = fields.Datetime("وقت الاعتماد", readonly=True, copy=False)
    transaction_ids = fields.One2many("alwabrn.transaction", "batch_id", "الحركات الناتجة")
    snapshot_ids = fields.One2many("alwabrn.daily.snapshot", "batch_id", "الكشوف الناتجة")
    content_hash = fields.Char("بصمة المحتوى", readonly=True, index=True, copy=False)
    duplicate_of_id = fields.Many2one("alwabrn.import.batch", "تكرار محتمل للدفعة", readonly=True, copy=False)
    allow_duplicate = fields.Boolean("تأكيد المدير: ليست مكررة", copy=False, tracking=True)
    external_message_id = fields.Char("معرف الرسالة الخارجي", readonly=True, index=True, copy=False)
    sender = fields.Char("المرسل", readonly=True)
    error_message = fields.Text("رسالة الخطأ", readonly=True)
    line_count = fields.Integer("عدد الأسطر", compute="_compute_counts")
    error_count = fields.Integer("أخطاء", compute="_compute_counts")
    warning_count = fields.Integer("تحذيرات", compute="_compute_counts")
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)

    @api.depends("line_ids.status", "line_ids.excluded")
    def _compute_counts(self):
        for rec in self:
            active = rec.line_ids.filtered(lambda l: not l.excluded)
            rec.line_count = len(active)
            rec.error_count = len(active.filtered(lambda l: l.status == "error"))
            rec.warning_count = len(active.filtered(lambda l: l.status == "warning"))

    def _compute_preview_image(self):
        for rec in self:
            img = rec.source_document_ids.filtered("is_image")[:1]
            rec.preview_image = img.datas if img else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "جديد") == "جديد":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("alwabrn.import.batch") or "جديد"
        return super().create(vals_list)

    def unlink(self):
        if any(r.state in ("approved", "posted") for r in self):
            raise UserError("لا يمكن حذف دفعة معتمدة أو مرحّلة.")
        return super().unlink()

    # ------------------------------------------------------- line building
    def _add_parsed_lines(self, parsed, confidences=None, page=None, document=None):
        """Create staging lines from text_parser output."""
        self.ensure_one()
        vals_list = []
        start = len(self.line_ids)
        for i, p in enumerate(parsed):
            conf = confidences[i] if confidences else 100.0
            vals_list.append({
                "batch_id": self.id, "line_no": start + i + 1, "raw_text": p.get("raw_text"),
                "sec_code": p.get("sec_code"), "description": p.get("description"),
                "quantity": p.get("quantity") or 0.0, "quantity_read": p.get("quantity") is not None,
                "unit_text": p.get("unit"), "line_date": p.get("date"), "site_ref": p.get("site_ref"),
                "work_order_ref": p.get("work_order_ref"), "document_ref": p.get("document_ref"),
                "detected_type": p.get("detected_type"), "confidence": conf,
                "parser_issues": ",".join(p.get("issues") or []),
                "source_page": page, "source_document_id": document.id if document else False,
            })
        self.env["alwabrn.import.line"].create(vals_list)

    def load_text(self, text):
        """Parse free text (paste / WhatsApp / e-mail body) into staging lines."""
        self.ensure_one()
        parsed = text_parser.parse_text(text)
        self.raw_text = ((self.raw_text or "") + "\n" + (text or "")).strip()
        self._add_parsed_lines(parsed)
        self._suggest_content_type()
        return len(parsed)

    def load_table(self, content, filename, mapping=None, header_row=None, document=None):
        """Load an XLSX/XLS/CSV table. ``mapping`` = {field: column index}."""
        self.ensure_one()
        rows = xlsx_utils.read_table(content, filename)
        det_header, det_map = xlsx_utils.detect_columns(rows)
        mapping = mapping or det_map
        header_row = det_header if header_row is None else header_row
        if "sec_code" not in mapping:
            raise UserError("تعذر تحديد عمود كود SEC. حدد الأعمدة يدوياً.")
        parsed = []
        for idx, row in enumerate(rows):
            if idx <= header_row:
                continue

            def cell(field):
                j = mapping.get(field)
                return row[j] if j is not None and j < len(row) else None
            sec = cell("sec_code")
            if isinstance(sec, float) and sec == int(sec):
                sec = int(sec)
            sec = str(sec).strip() if sec not in (None, "") else None
            qty = cell("quantity")
            if sec is None and qty in (None, ""):
                continue
            issues = []
            qty_val = None
            if isinstance(qty, (int, float)) and not isinstance(qty, bool):
                qty_val = float(qty)
            elif isinstance(qty, str) and qty.strip():
                try:
                    qty_val = float(text_parser.normalize(qty).replace(",", ""))
                except ValueError:
                    issues.append("bad_quantity")
            else:
                issues.append("no_quantity")
            if not sec:
                issues.append("no_sec_code")
            d = cell("date")
            line_date = None
            if hasattr(d, "date"):
                line_date = d.date()
            elif hasattr(d, "year"):
                line_date = d
            elif isinstance(d, str) and d.strip():
                line_date, _span = text_parser.parse_date(text_parser.normalize(d))
                if not line_date:
                    issues.append("bad_date")
            site = cell("site")
            parsed.append({
                "raw_text": " | ".join("" if v is None else str(v) for v in row)[:500],
                "sec_code": sec, "quantity": qty_val, "unit": (str(cell("unit")).strip() if cell("unit") else None),
                "date": line_date, "site_ref": (str(int(site)) if isinstance(site, float) and site == int(site)
                                                else str(site).strip()) if site not in (None, "") else None,
                "document_ref": str(cell("document")).strip() if cell("document") else None,
                "description": str(cell("description")).strip() if cell("description") else None,
                "detected_type": None, "issues": issues,
            })
        self._add_parsed_lines(parsed, document=document)
        return len(parsed)

    def load_document(self, document):
        """Route a source document: spreadsheet -> table loader, image/pdf -> OCR."""
        self.ensure_one()
        document.batch_id = self
        if document.duplicate_of_id and not self.duplicate_of_id:
            self.duplicate_of_id = document.duplicate_of_id.batch_id
        mime = document.mimetype or ""
        if "spreadsheet" in mime or "excel" in mime or mime == "text/csv":
            return self.load_table(document._raw(), document.name, document=document)
        extractions = self.env["alwabrn.ocr.extraction"].run(document)
        count = 0
        for ext in extractions:
            if ext.state != "done":
                self.error_message = "تعذر استخراج النص آلياً (%s): %s — أدخل الأسطر يدوياً بجانب الصورة." % (
                    ext.provider, ext.error or "")
                continue
            lines = ext.line_data or []
            parsed, confs = [], []
            for item in lines:
                p = text_parser.parse_line(item["text"])
                if not p or (p["sec_code"] is None and p["quantity"] is None):
                    continue
                parsed.append(p)
                confs.append(item.get("conf", 0.0))
            self._add_parsed_lines(parsed, confidences=confs, page=ext.page, document=document)
            count += len(parsed)
        self._suggest_content_type()
        return count

    def _suggest_content_type(self):
        """Only suggest when every line agrees; otherwise the user must classify."""
        for rec in self.filtered(lambda r: r.content_type == "unknown"):
            kinds = {l.detected_type for l in rec.line_ids if l.detected_type}
            if len(kinds) == 1 and all(l.detected_type for l in rec.line_ids):
                mapped = DETECTED_MAP.get(kinds.pop())
                if mapped:
                    rec.content_type = mapped

    # ------------------------------------------------------------ workflow
    def _compute_content_hash(self):
        for rec in self:
            sig = "|".join(sorted("%s:%s:%s:%s" % (l.sec_code, l.quantity, l.unit_text or "", l.site_ref or "")
                                  for l in rec.line_ids if not l.excluded))
            rec.content_hash = hashlib.sha256(
                ("%s|%s|%s" % (rec.content_type, rec.effective_date, sig)).encode()).hexdigest() if sig else False

    def action_validate(self):
        for rec in self:
            if rec.state in ("approved", "posted"):
                continue
            rec.state = "processing"
            rec.line_ids._validate()
            rec._compute_content_hash()
            if rec.content_hash and not rec.duplicate_of_id:
                rec.duplicate_of_id = self.search([
                    ("content_hash", "=", rec.content_hash), ("id", "!=", rec.id),
                    ("state", "in", ("approved", "posted"))], limit=1)
            needs_review = (rec.content_type == "unknown" or rec.error_count or rec.warning_count
                            or rec.duplicate_of_id or not rec.line_count)
            rec.state = "needs_review" if needs_review else "validated"
        return True

    def action_approve(self):
        """Manager approval. Stock types are posted through Odoo; balance
        statements only create a snapshot (official stock is NOT changed)."""
        self.env["alwabrn.stock.service"].require_manager()
        for rec in self:
            rec.flush_recordset()
            self.env.cr.execute("SELECT state FROM alwabrn_import_batch WHERE id=%s FOR UPDATE", [rec.id])
            if self.env.cr.fetchone()[0] in ("approved", "posted"):
                continue  # idempotent retry
            rec.invalidate_recordset(["state"])
            rec.action_validate()
            if rec.content_type == "unknown":
                raise UserError("صنّف نوع البيانات أولاً (رصيد دفتري / استلام / صرف ...). لا يفترض النظام أن الكميات استلام.")
            lines = rec.line_ids.filtered(lambda l: not l.excluded)
            if not lines:
                raise UserError("لا توجد أسطر صالحة.")
            if rec.error_count:
                raise UserError("توجد %s أسطر بها أخطاء. صححها أو استبعدها قبل الاعتماد." % rec.error_count)
            unconfirmed = lines.filtered(lambda l: l.status == "warning" and not l.reviewed)
            if unconfirmed:
                raise UserError("توجد %s أسطر بها تحذيرات لم تُراجع (مثل ثقة OCR منخفضة). ضع علامة «تمت المراجعة» بعد التأكد." % len(unconfirmed))
            if rec.duplicate_of_id and not rec.allow_duplicate:
                raise UserError("هذه الدفعة تبدو مكررة للدفعة %s. إن كانت حركة حقيقية جديدة فعّل «تأكيد المدير: ليست مكررة»." % rec.duplicate_of_id.name)
            if rec.duplicate_of_id:
                self.env["alwabrn.approval"].log(rec, "duplicate_override", "تجاوز تحذير التكرار")
            if rec.content_type in SNAPSHOT_TYPES:
                rec._create_snapshot(lines)
                state = "approved"
            else:
                rec._create_transactions(lines)
                state = "posted"
            rec.write({"state": state, "approved_by": self.env.user.id, "approved_on": fields.Datetime.now()})
            self.env["alwabrn.approval"].log(rec, "approve", details={"type": rec.content_type, "lines": len(lines)})
        return True

    def action_reject(self):
        self.env["alwabrn.stock.service"].require_manager()
        for rec in self.filtered(lambda r: r.state not in ("approved", "posted")):
            rec.state = "rejected"
            self.env["alwabrn.approval"].log(rec, "reject")
        return True

    def action_reset(self):
        self.filtered(lambda r: r.state in ("rejected", "failed", "needs_review", "validated")).write({"state": "draft"})
        return True

    def _create_snapshot(self, lines):
        self.ensure_one()
        merged = {}
        for l in lines:
            key = (l.product_id.id, l.uom_id.id)
            merged[key] = merged.get(key, 0.0) + l.quantity
        by_product = {}
        for (pid, uid), qty in merged.items():
            if pid in by_product:
                raise UserError("المادة مكررة بوحدتين مختلفتين في نفس الكشف.")
            by_product[pid] = (uid, qty)
        return self.env["alwabrn.daily.snapshot"].create({
            "snapshot_type": SNAPSHOT_TYPES[self.content_type], "balance_date": self.effective_date,
            "location_id": self.location_id.id, "batch_id": self.id, "submitted_by": self.submitted_by.id,
            "source_document_ids": [(6, 0, self.source_document_ids.ids)], "state": "approved",
            "note": "من الدفعة %s" % self.name,
            "line_ids": [(0, 0, {"product_id": pid, "uom_id": uid, "quantity": qty})
                         for pid, (uid, qty) in by_product.items()],
        })

    def _create_transactions(self, lines):
        self.ensure_one()
        groups = {}
        for l in lines:
            key = (l.line_date or self.effective_date, l.site_id.id or self.site_id.id or False)
            groups.setdefault(key, self.env["alwabrn.import.line"])
            groups[key] |= l
        Tx = self.env["alwabrn.transaction"]
        for n, ((day, site_id), glines) in enumerate(sorted(groups.items(), key=lambda g: str(g[0])), start=1):
            dt = fields.Datetime.now() if day == riyadh_today() else \
                fields.Datetime.to_datetime(day).replace(hour=9)  # 12:00 Riyadh
            tx = Tx.create({
                "tx_type": self.content_type, "date": dt, "site_id": site_id,
                "document_ref": self.document_ref or glines[:1].document_ref, "batch_id": self.id,
                "source_document_ids": [(6, 0, self.source_document_ids.ids)],
                "idempotency_key": "BATCH-%s-%s" % (self.id, n),
                "note": "من الدفعة %s (%s)" % (self.name, dict(CHANNELS).get(self.channel)),
                "line_ids": [(0, 0, {"product_id": l.product_id.id, "quantity": l.quantity,
                                     "uom_id": l.uom_id.id, "note": l.note}) for l in glines],
            })
            tx._check_ready()
            tx._post()

    # -------------------------------------------------------------- report
    def action_download_validation_report(self):
        self.ensure_one()
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False})
        ws = wb.add_worksheet("تقرير التحقق")
        ws.right_to_left()
        head = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white", "border": 1})
        bad = wb.add_format({"bg_color": "#F8CBAD"})
        warn = wb.add_format({"bg_color": "#FFE699"})
        cols = ["#", "النص الأصلي", "كود SEC", "المادة", "الكمية", "الوحدة", "الحالة", "الملاحظات", "مستبعد"]
        for c, h in enumerate(cols):
            ws.write(0, c, h, head)
        for r, l in enumerate(self.line_ids, start=1):
            fmt = bad if l.status == "error" else warn if l.status == "warning" else None
            vals = [l.line_no, l.raw_text, l.sec_code, l.product_id.name, l.quantity, l.unit_text or l.uom_id.name,
                    dict(l._fields["status"].selection).get(l.status), l.issue_text, "نعم" if l.excluded else ""]
            for c, v in enumerate(vals):
                ws.write(r, c, xlsx_utils.safe_cell(v) if v not in (None, False) else "", fmt)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, max(len(self.line_ids), 1), len(cols) - 1)
        ws.set_column(0, 0, 5); ws.set_column(1, 1, 45); ws.set_column(2, 2, 14)
        ws.set_column(3, 3, 45); ws.set_column(4, 6, 12); ws.set_column(7, 7, 50)
        wb.close()
        att = self.env["ir.attachment"].create({
            "name": "validation_%s.xlsx" % self.name.replace("/", "_"), "type": "binary",
            "datas": base64.b64encode(buf.getvalue()), "res_model": self._name, "res_id": self.id})
        return {"type": "ir.actions.act_url", "url": "/web/content/%s?download=true" % att.id, "target": "self"}

    # ------------------------------------------------------- e-mail gateway
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Incoming e-mail (fetchmail / alias) -> draft import batch. Never posted automatically."""
        Account = self.env["alwabrn.integration.account"].sudo()
        Message = self.env["alwabrn.integration.message"].sudo()
        message_id = msg_dict.get("message_id")
        sender = (msg_dict.get("email_from") or "").strip()
        existing = Message.search([("external_id", "=", message_id), ("channel", "=", "email")], limit=1) if message_id else Message
        if existing and existing.batch_id:
            Account.log_event("email", "info", "رسالة مكررة تم تجاهلها: %s" % message_id)
            return existing.batch_id.id
        authorized = Account.is_email_sender_allowed(sender)
        body = re.sub(r"<[^>]+>", "\n", msg_dict.get("body") or "")
        batch = self.sudo().create(dict(custom_values or {}, **{
            "subject": msg_dict.get("subject") or "بريد وارد", "channel": "email",
            "external_message_id": message_id, "sender": sender,
            "state": "draft" if authorized else "rejected",
            "error_message": False if authorized else "المرسل غير مصرح به — لم تتم معالجة المحتوى.",
        }))
        Message.create({"channel": "email", "external_id": message_id or "nomsgid-%s" % batch.id,
                        "sender": sender, "body": body[:5000], "batch_id": batch.id,
                        "state": "processed" if authorized else "unauthorized"})
        if not authorized:
            Account.log_event("email", "warning", "بريد من مرسل غير مصرح: %s" % sender)
            return batch.id
        try:
            for att in msg_dict.get("attachments") or []:
                fname, content = att[0], att[1]
                if isinstance(content, str):
                    content = content.encode()
                low = (fname or "").lower()
                if not low.endswith((".xlsx", ".xls", ".csv", ".pdf", ".jpg", ".jpeg", ".png", ".heic")):
                    continue
                doc = self.env["alwabrn.source.document"].sudo().create({
                    "name": fname, "datas": base64.b64encode(content), "channel": "email",
                    "external_ref": message_id})
                batch.load_document(doc)
            if not batch.line_ids:
                batch.load_text(body)
            batch.action_validate()
            Account.log_event("email", "info", "تم إنشاء دفعة %s من بريد %s" % (batch.name, sender), ok=True)
        except Exception as e:
            batch.write({"state": "failed", "error_message": str(e)})
            Account.log_event("email", "error", "فشل معالجة بريد %s: %s" % (sender, e))
        return batch.id


class AlwabrnImportLine(models.Model):
    _name = "alwabrn.import.line"
    _description = "سطر إدخال (مرحلي)"
    _order = "batch_id, line_no, id"

    batch_id = fields.Many2one("alwabrn.import.batch", required=True, ondelete="cascade", index=True)
    line_no = fields.Integer("#")
    raw_text = fields.Char("النص الأصلي")
    sec_code = fields.Char("كود SEC", index=True)
    product_id = fields.Many2one("product.product", "المادة المطابقة", domain=[("alw_is_material", "=", True)])
    description = fields.Char("الوصف المستخرج")
    quantity = fields.Float("الكمية", digits="Product Unit")
    quantity_read = fields.Boolean("الكمية مقروءة", default=True)
    unit_text = fields.Char("الوحدة (نص)")
    uom_id = fields.Many2one("uom.uom", "الوحدة")
    qty_product_uom = fields.Float("الكمية بالوحدة الأساسية", digits=(16, 6), readonly=True)
    product_uom_id = fields.Many2one("uom.uom", related="product_id.uom_id", string="وحدة المادة")
    line_date = fields.Date("التاريخ")
    site_ref = fields.Char("الموقع (نص)")
    site_id = fields.Many2one("alwabrn.site", "الموقع")
    work_order_ref = fields.Char("أمر العمل")
    document_ref = fields.Char("رقم المستند")
    detected_type = fields.Char("النوع المكتشف", readonly=True)
    confidence = fields.Float("الثقة %", default=100.0)
    parser_issues = fields.Char(readonly=True)
    issue_codes = fields.Char("رموز الملاحظات", readonly=True)
    issue_text = fields.Char("الملاحظات", readonly=True)
    status = fields.Selection([("new", "جديد"), ("ok", "سليم"), ("warning", "تحذير"), ("error", "خطأ")],
                              "الحالة", default="new", readonly=True, index=True)
    reviewed = fields.Boolean("تمت المراجعة")
    excluded = fields.Boolean("مستبعد")
    note = fields.Char("ملاحظة")
    source_page = fields.Integer("الصفحة")
    source_document_id = fields.Many2one("alwabrn.source.document", "المستند المصدر")

    @api.onchange("product_id")
    def _onchange_product(self):
        if self.product_id:
            self.sec_code = self.product_id.alw_sec_code
            if not self.uom_id:
                self.uom_id = self.product_id.uom_id

    def write(self, vals):
        if any(l.batch_id.state in ("approved", "posted") for l in self) and not self.env.su:
            raise UserError("لا يمكن تعديل أسطر دفعة معتمدة.")
        if "product_id" in vals and vals["product_id"] and "sec_code" not in vals:
            vals["sec_code"] = self.env["product.product"].browse(vals["product_id"]).alw_sec_code
        return super().write(vals)

    def _validate(self):
        svc = self.env["alwabrn.stock.service"]
        Product = self.env["product.template"]
        Site = self.env["alwabrn.site"]
        for batch in self.batch_id:
            lines = self.filtered(lambda l: l.batch_id == batch)
            seen = {}
            for l in lines:
                codes = set(filter(None, (l.parser_issues or "").split(",")))
                if l.quantity_read and l.quantity > 0:
                    codes.discard("no_quantity"); codes.discard("bad_quantity")
                vals = {}
                # --- material: never invent a code
                product = l.product_id
                if l.sec_code and (not product or product.alw_sec_code != l.sec_code.strip()):
                    product = Product.alw_find_by_sec(l.sec_code)
                    vals["product_id"] = product.id
                if product:
                    codes.discard("no_sec_code")
                elif l.sec_code:
                    codes.add("unknown_material")
                else:
                    codes.add("no_sec_code")
                # --- quantity
                if l.quantity <= 0:
                    codes.add("bad_quantity")
                elif l.quantity > 1000000:
                    codes.add("suspicious_qty")
                # --- unit
                uom = l.uom_id
                if l.unit_text and not uom:
                    uom = svc.uom_from_text(l.unit_text)
                    if not uom:
                        codes.add("bad_unit")
                if product and not uom and "bad_unit" not in codes:
                    uom = product.uom_id
                if uom:
                    vals["uom_id"] = uom.id
                if product and uom:
                    if uom != product.uom_id and not uom._has_common_reference(product.uom_id):
                        codes.add("unit_incompatible")
                        vals["qty_product_uom"] = 0.0
                    else:
                        vals["qty_product_uom"] = svc.convert_qty(l.quantity, uom, product)
                # --- site
                site = l.site_id
                if l.site_ref and not site:
                    site = Site.search([("code", "=", l.site_ref.strip())], limit=1)
                    if site:
                        vals["site_id"] = site.id
                    else:
                        codes.add("site_unknown")
                if batch.content_type in ("issue_site", "return_in", "consume") and not (site or batch.site_id):
                    codes.add("site_missing")
                # --- date
                if l.line_date and l.line_date > riyadh_today():
                    codes.add("bad_date")
                # --- OCR confidence
                if l.confidence < LOW_CONFIDENCE:
                    codes.add("low_confidence")
                # --- duplicates inside the batch
                key = (l.sec_code, l.quantity, l.site_ref or "", l.line_date)
                if not l.excluded:
                    if key in seen and l.sec_code:
                        codes.add("dup_in_batch")
                    seen[key] = True
                # --- possible duplicate of an already posted movement (flag only)
                if product and batch.content_type in STOCK_TYPES and l.quantity > 0:
                    day = l.line_date or batch.effective_date
                    dup = self.env["alwabrn.transaction.line"].sudo().search_count([
                        ("product_id", "=", product.id), ("quantity", "=", l.quantity),
                        ("tx_type", "=", batch.content_type), ("state", "=", "posted"),
                        ("transaction_id.batch_id", "!=", batch.id),
                        ("date", ">=", fields.Datetime.to_datetime(day)),
                        ("date", "<", fields.Datetime.add(fields.Datetime.to_datetime(day), days=1))])
                    if dup:
                        codes.add("possible_duplicate")
                status = "error" if codes & ERROR_CODES else "warning" if codes else "ok"
                vals.update({"issue_codes": ",".join(sorted(codes)), "status": status,
                             "issue_text": "؛ ".join(ISSUE_LABELS.get(c, c) for c in sorted(codes))})
                super(AlwabrnImportLine, l).write(vals)
