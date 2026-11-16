from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

from .common import riyadh_day_end_utc, riyadh_today

CLASSIFICATIONS = [
    ("match", "مطابق"),
    ("missing_receipt", "استلام غير مسجل"),
    ("missing_issue", "صرف غير مسجل"),
    ("duplicate_tx", "حركة مكررة"),
    ("wrong_qty", "كمية خاطئة"),
    ("wrong_code", "كود SEC خاطئ"),
    ("wrong_unit", "وحدة خاطئة"),
    ("wrong_location", "موقع خاطئ"),
    ("unconfirmed", "حركة غير معتمدة"),
    ("date_mismatch", "اختلاف تاريخ"),
    ("unexplained", "فرق غير مفسر"),
]


class AlwabrnReconciliation(models.Model):
    _name = "alwabrn.reconciliation"
    _description = "مطابقة وحصر دفتري"
    _inherit = ["mail.thread"]
    _order = "date desc, id desc"

    name = fields.Char("المرجع", readonly=True, copy=False, default="جديد")
    date = fields.Date("تاريخ المطابقة", required=True, default=lambda s: riyadh_today(), tracking=True, index=True)
    cutoff = fields.Datetime("وقت القطع (UTC)", compute="_compute_cutoff", store=True, readonly=False)
    location_id = fields.Many2one("stock.location", "المستودع / الموقع", required=True,
                                  default=lambda s: s.env["alwabrn.stock.service"].main_stock_location(),
                                  domain=[("usage", "=", "internal")])
    book_snapshot_id = fields.Many2one("alwabrn.daily.snapshot", "كشف الرصيد الدفتري",
                                       domain=[("snapshot_type", "=", "book"), ("state", "=", "approved")])
    physical_snapshot_id = fields.Many2one("alwabrn.daily.snapshot", "كشف الجرد الفعلي",
                                           domain=[("snapshot_type", "=", "physical"), ("state", "=", "approved")])
    scope = fields.Selection([("listed", "المواد الواردة بالكشوف فقط"), ("all", "كل المواد ذات الرصيد")],
                             "النطاق", default="listed", required=True)
    state = fields.Selection([("draft", "مسودة"), ("computed", "تم الاحتساب"), ("closed", "مغلقة")],
                             default="draft", required=True, tracking=True, index=True)
    line_ids = fields.One2many("alwabrn.reconciliation.line", "reconciliation_id", "البنود")
    match_count = fields.Integer("مطابق", compute="_compute_stats", store=True)
    diff_count = fields.Integer("فروقات", compute="_compute_stats", store=True)
    incomplete_count = fields.Integer("بيانات ناقصة", compute="_compute_stats", store=True)
    computed_on = fields.Datetime("وقت الاحتساب", readonly=True)
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)

    @api.depends("date")
    def _compute_cutoff(self):
        for rec in self:
            rec.cutoff = riyadh_day_end_utc(rec.date) if rec.date else False

    @api.depends("line_ids.status")
    def _compute_stats(self):
        for rec in self:
            rec.match_count = len(rec.line_ids.filtered(lambda l: l.status == "match"))
            rec.diff_count = len(rec.line_ids.filtered(lambda l: l.status == "diff"))
            rec.incomplete_count = len(rec.line_ids.filtered(lambda l: l.status == "incomplete"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "جديد") == "جديد":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("alwabrn.reconciliation") or "جديد"
        return super().create(vals_list)

    def action_compute(self):
        """Compare Odoo balance (at the cutoff) with book and/or physical figures.
        Missing information is stored as *missing*, never as zero. Stock is not touched."""
        svc = self.env["alwabrn.stock.service"]
        Line = self.env["alwabrn.reconciliation.line"]
        for rec in self:
            if rec.state == "closed":
                raise UserError("المطابقة مغلقة.")
            if not rec.book_snapshot_id and not rec.physical_snapshot_id:
                raise UserError("حدد كشف الرصيد الدفتري أو كشف الجرد الفعلي (مصدران على الأقل للمقارنة).")
            rec.line_ids.filtered(lambda l: l.resolution == "open").unlink()
            kept = {l.product_id.id for l in rec.line_ids}
            odoo = svc.balance_at(rec.location_id, rec.cutoff)
            book = {l.product_id.id: l.qty_product_uom for l in rec.book_snapshot_id.line_ids}
            phys = {l.product_id.id: l.qty_product_uom for l in rec.physical_snapshot_id.line_ids}
            product_ids = set(book) | set(phys)
            if rec.scope == "all":
                product_ids |= {pid for pid, q in odoo.items() if not float_is_zero(q, precision_digits=6)}
            vals_list = []
            for product in self.env["product.product"].browse(sorted(product_ids - kept)):
                vals_list.append({
                    "reconciliation_id": rec.id, "product_id": product.id,
                    "odoo_qty": odoo.get(product.id, 0.0),
                    "has_book": product.id in book, "book_qty": book.get(product.id, 0.0),
                    "has_physical": product.id in phys, "physical_qty": phys.get(product.id, 0.0),
                })
            lines = Line.create(vals_list)
            lines._suggest_classification()
            rec.write({"state": "computed", "computed_on": fields.Datetime.now()})
        return True

    def action_close(self):
        self.env["alwabrn.stock.service"].require_manager()
        self.write({"state": "closed"})
        for rec in self:
            self.env["alwabrn.approval"].log(rec, "approve", "إغلاق المطابقة", {
                "match": rec.match_count, "diff": rec.diff_count})
        return True


class AlwabrnReconciliationLine(models.Model):
    _name = "alwabrn.reconciliation.line"
    _description = "بند مطابقة"
    _order = "reconciliation_id, status, sec_code"

    reconciliation_id = fields.Many2one("alwabrn.reconciliation", required=True, ondelete="cascade", index=True)
    date = fields.Date(related="reconciliation_id.date", store=True, index=True)
    location_id = fields.Many2one(related="reconciliation_id.location_id", store=True)
    product_id = fields.Many2one("product.product", "المادة", required=True, index=True)
    sec_code = fields.Char("كود SEC", related="product_id.alw_sec_code", store=True)
    uom_id = fields.Many2one("uom.uom", "الوحدة", related="product_id.uom_id")
    odoo_qty = fields.Float("رصيد Odoo", digits=(16, 4), readonly=True)
    has_book = fields.Boolean("يوجد رصيد دفتري", readonly=True)
    book_qty = fields.Float("الرصيد الدفتري", digits=(16, 4), readonly=True)
    has_physical = fields.Boolean("يوجد جرد فعلي", readonly=True)
    physical_qty = fields.Float("الجرد الفعلي", digits=(16, 4), readonly=True)
    diff_book = fields.Float("فرق الدفتري - Odoo", compute="_compute_diffs", store=True, digits=(16, 4))
    diff_physical = fields.Float("فرق الفعلي - Odoo", compute="_compute_diffs", store=True, digits=(16, 4))
    diff_book_physical = fields.Float("فرق الفعلي - الدفتري", compute="_compute_diffs", store=True, digits=(16, 4))
    difference = fields.Float("الفرق", compute="_compute_diffs", store=True, digits=(16, 4),
                              help="الفعلي - Odoo إن وجد الجرد الفعلي، وإلا الدفتري - Odoo.")
    status = fields.Selection([("match", "مطابق"), ("diff", "فرق"), ("incomplete", "بيانات ناقصة")],
                              "الحالة", compute="_compute_diffs", store=True, index=True)
    classification = fields.Selection(CLASSIFICATIONS, "تصنيف الفرق")
    classification_auto = fields.Boolean("تصنيف مقترح آلياً", readonly=True)
    analysis = fields.Text("تحليل النظام", readonly=True)
    note = fields.Text("ملاحظات المراجع")
    resolution = fields.Selection([("open", "مفتوح"), ("explained", "تم التفسير"),
                                   ("adjust_pending", "تسوية بانتظار الاعتماد"), ("adjusted", "تمت التسوية")],
                                  "المعالجة", default="open", required=True, index=True)
    adjustment_tx_id = fields.Many2one("alwabrn.transaction", "حركة التسوية", readonly=True)

    @api.depends("odoo_qty", "book_qty", "physical_qty", "has_book", "has_physical")
    def _compute_diffs(self):
        for l in self:
            rounding = l.product_id.uom_id.rounding or 0.01
            l.diff_book = (l.book_qty - l.odoo_qty) if l.has_book else 0.0
            l.diff_physical = (l.physical_qty - l.odoo_qty) if l.has_physical else 0.0
            l.diff_book_physical = (l.physical_qty - l.book_qty) if (l.has_book and l.has_physical) else 0.0
            if not l.has_book and not l.has_physical:
                l.difference, l.status = 0.0, "incomplete"
                continue
            l.difference = l.diff_physical if l.has_physical else l.diff_book
            diffs = []
            if l.has_book:
                diffs.append(l.diff_book)
            if l.has_physical:
                diffs.append(l.diff_physical)
            l.status = "match" if all(float_is_zero(d, precision_rounding=rounding) for d in diffs) else "diff"

    def _suggest_classification(self):
        """Evidence-based suggestion only; the reviewer has the final word."""
        TxLine = self.env["alwabrn.transaction.line"].sudo()
        for l in self:
            if l.status == "match":
                l.write({"classification": "match", "classification_auto": True, "analysis": False})
                continue
            if l.status != "diff":
                continue
            rec = l.reconciliation_id
            diff = l.difference
            absd = abs(diff)
            rounding = l.product_id.uom_id.rounding or 0.01
            eq = lambda a, b: float_compare(a, b, precision_rounding=rounding) == 0  # noqa: E731
            base = [("product_id", "=", l.product_id.id)]
            notes, cls = [], None
            pending = TxLine.search(base + [("state", "in", ("draft", "pending"))])
            hit = pending.filtered(lambda t: eq(t.qty_product_uom, absd))
            if hit:
                cls = "unconfirmed"
                notes.append("توجد حركة غير معتمدة بنفس كمية الفرق: %s" % ", ".join(hit.mapped("transaction_id.name")))
            posted = TxLine.search(base + [("state", "=", "posted")])
            if not cls:
                after = posted.filtered(lambda t: t.date > rec.cutoff and eq(t.qty_product_uom, absd))
                late = posted.filtered(lambda t: t.date <= rec.cutoff and t.transaction_id.approved_on
                                       and t.transaction_id.approved_on > rec.cutoff and eq(t.qty_product_uom, absd))
                if after or late:
                    cls = "date_mismatch"
                    notes.append("حركة بنفس كمية الفرق مسجلة بتاريخ مختلف عن وقت القطع: %s" % ", ".join(
                        (after | late).mapped("transaction_id.name")))
            if not cls:
                seen = {}
                for t in posted.filtered(lambda t: t.date <= rec.cutoff):
                    key = (t.tx_type, fields.Date.to_date(t.date), round(t.qty_product_uom, 4), t.site_id.id)
                    seen.setdefault(key, []).append(t)
                for key, items in seen.items():
                    if len(items) > 1 and eq(key[2], absd):
                        cls = "duplicate_tx"
                        notes.append("حركات متطابقة قد تكون مكررة: %s" % ", ".join(
                            t.transaction_id.name for t in items))
                        break
            if not cls and l.odoo_qty and (l.has_physical or l.has_book):
                other = l.physical_qty if l.has_physical else l.book_qty
                if other and (eq(other, l.odoo_qty * 1000) or eq(other * 1000, l.odoo_qty)):
                    cls = "wrong_unit"
                    notes.append("النسبة بين الرصيدين 1000 — يحتمل إدخال بوحدة مختلفة (كم/متر).")
            if not cls:
                cls = "missing_receipt" if diff > 0 else "missing_issue"
                src = "الفعلي" if l.has_physical else "الدفتري"
                if diff > 0:
                    notes.append("الرصيد %s أعلى من Odoo بمقدار %s — يحتمل استلام غير مسجل. يحتاج تحقق من المستندات." % (src, absd))
                else:
                    notes.append("رصيد Odoo أعلى من %s بمقدار %s — يحتمل صرف غير مسجل. يحتاج تحقق من المستندات." % (src, absd))
            l.write({"classification": cls, "classification_auto": True, "analysis": "\n".join(notes)})

    def action_view_movements(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": "حركات المادة %s" % self.sec_code,
                "res_model": "alwabrn.stock.card.line", "view_mode": "list",
                "domain": [("product_id", "=", self.product_id.id)]}

    def action_mark_explained(self):
        for l in self:
            if not l.note:
                raise UserError("اكتب تفسير الفرق في الملاحظات أولاً.")
            l.resolution = "explained"
        return True

    def action_request_adjustment(self):
        """Create a *pending* adjustment for manager approval. Target = physical
        count when available, otherwise the book balance. Nothing is posted here."""
        self.ensure_one()
        if self.status != "diff" or self.resolution not in ("open", "explained"):
            raise UserError("لا يمكن طلب تسوية لهذا البند.")
        if not self.note:
            raise UserError("سبب التسوية إلزامي (اكتبه في الملاحظات).")
        svc = self.env["alwabrn.stock.service"]
        target = self.physical_qty if self.has_physical else self.book_qty
        current = svc.current_qty(self.product_id, self.location_id)
        # movements after the cutoff stay valid: adjust by the cutoff difference
        delta = target - self.odoo_qty
        if float_is_zero(delta, precision_digits=6):
            raise UserError("لا يوجد فرق للتسوية.")
        tx = self.env["alwabrn.transaction"].create({
            "tx_type": "adj_plus" if delta > 0 else "adj_minus",
            "reason": "تسوية جردية من المطابقة %s: %s" % (self.reconciliation_id.name, self.note),
            "reconciliation_line_id": self.id, "idempotency_key": "RECON-%s" % self.id,
            "source_document_ids": [(6, 0, (self.reconciliation_id.physical_snapshot_id.source_document_ids
                                            | self.reconciliation_id.book_snapshot_id.source_document_ids).ids)],
            "line_ids": [(0, 0, {"product_id": self.product_id.id, "quantity": abs(delta),
                                 "uom_id": self.product_id.uom_id.id})],
        })
        tx.write({"location_src_id" if delta < 0 else "location_dest_id": self.location_id.id})
        tx.action_submit()
        self.write({"adjustment_tx_id": tx.id, "resolution": "adjust_pending"})
        self.env["alwabrn.approval"].log(self.reconciliation_id, "adjust", self.note, {
            "sec": self.sec_code, "previous_balance": current, "odoo_at_cutoff": self.odoo_qty,
            "book": self.book_qty if self.has_book else None,
            "physical": self.physical_qty if self.has_physical else None,
            "difference": self.difference, "adjustment_qty": delta, "transaction": tx.name})
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.transaction",
                "res_id": tx.id, "view_mode": "form"}


class AlwabrnTransactionRecon(models.Model):
    _inherit = "alwabrn.transaction"

    def _post(self):
        res = super()._post()
        for rec in self.filtered(lambda r: r.reconciliation_line_id and r.state == "posted"):
            rec.reconciliation_line_id.resolution = "adjusted"
        return res
