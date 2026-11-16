from odoo import api, fields, models
from odoo.exceptions import UserError

from .common import riyadh_day_end_utc, riyadh_today


class AlwabrnDailySnapshot(models.Model):
    """Book balance statement / physical count. NEVER a stock movement."""
    _name = "alwabrn.daily.snapshot"
    _description = "كشف رصيد يومي / جرد فعلي"
    _inherit = ["mail.thread"]
    _order = "balance_date desc, id desc"

    name = fields.Char("المرجع", readonly=True, copy=False, default="جديد")
    snapshot_type = fields.Selection([("book", "رصيد دفتري"), ("physical", "جرد فعلي")],
                                     "نوع الكشف", required=True, default="book", tracking=True, index=True)
    balance_date = fields.Date("تاريخ الرصيد", required=True, default=lambda s: riyadh_today(), tracking=True, index=True)
    cutoff = fields.Datetime("وقت القطع (UTC)", compute="_compute_cutoff", store=True, readonly=False,
                             help="الحركات حتى هذا الوقت فقط تدخل في المقارنة. الافتراضي نهاية اليوم بتوقيت الرياض.")
    location_id = fields.Many2one("stock.location", "المستودع / الموقع", required=True,
                                  default=lambda s: s.env["alwabrn.stock.service"].main_stock_location(),
                                  domain=[("usage", "=", "internal")])
    source_document_ids = fields.Many2many("alwabrn.source.document", string="المستندات المصدر")
    batch_id = fields.Many2one("alwabrn.import.batch", "دفعة الإدخال", readonly=True, index="btree_not_null")
    submitted_by = fields.Many2one("res.users", "مقدم الكشف", default=lambda s: s.env.user, tracking=True)
    state = fields.Selection([("draft", "مسودة"), ("approved", "معتمد"), ("cancelled", "ملغي")],
                             default="draft", required=True, tracking=True, index=True)
    note = fields.Text("ملاحظات")
    line_ids = fields.One2many("alwabrn.snapshot.line", "snapshot_id", "الأصناف", copy=True)
    line_count = fields.Integer(compute="_compute_line_count")
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)

    @api.depends("balance_date")
    def _compute_cutoff(self):
        for rec in self:
            rec.cutoff = riyadh_day_end_utc(rec.balance_date) if rec.balance_date else False

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "جديد") == "جديد":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("alwabrn.snapshot") or "جديد"
        return super().create(vals_list)

    def action_approve(self):
        self.env["alwabrn.stock.service"].require_manager()
        for rec in self:
            if not rec.line_ids:
                raise UserError("لا توجد أصناف في الكشف.")
            rec.line_ids._check_convertible()
            rec.state = "approved"
            self.env["alwabrn.approval"].log(rec, "approve", "اعتماد كشف (لا يغير المخزون)")
        return True

    def action_cancel(self):
        self.env["alwabrn.stock.service"].require_manager()
        self.write({"state": "cancelled"})
        return True

    def action_reconcile(self):
        self.ensure_one()
        if self.state != "approved":
            raise UserError("اعتمد الكشف أولاً.")
        vals = {"date": self.balance_date, "cutoff": self.cutoff, "location_id": self.location_id.id}
        vals["book_snapshot_id" if self.snapshot_type == "book" else "physical_snapshot_id"] = self.id
        rec = self.env["alwabrn.reconciliation"].create(vals)
        rec.action_compute()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.reconciliation",
                "res_id": rec.id, "view_mode": "form"}

    def unlink(self):
        if any(r.state == "approved" for r in self):
            raise UserError("لا يمكن حذف كشف معتمد (سجل تاريخي). يمكن إلغاؤه.")
        return super().unlink()


class AlwabrnSnapshotLine(models.Model):
    _name = "alwabrn.snapshot.line"
    _description = "بند كشف رصيد"
    _order = "snapshot_id, id"

    snapshot_id = fields.Many2one("alwabrn.daily.snapshot", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", "المادة", required=True, index=True,
                                 domain=[("alw_is_material", "=", True)])
    sec_code = fields.Char("كود SEC", related="product_id.alw_sec_code", store=True)
    quantity = fields.Float("الكمية المدخلة", required=True, digits="Product Unit")
    uom_id = fields.Many2one("uom.uom", "الوحدة المدخلة", required=True, compute="_compute_uom",
                             store=True, readonly=False, precompute=True)
    qty_product_uom = fields.Float("الكمية بالوحدة الأساسية", compute="_compute_qty", store=True, digits=(16, 6))
    product_uom_id = fields.Many2one("uom.uom", related="product_id.uom_id", string="الوحدة الأساسية")
    note = fields.Char("ملاحظة")
    balance_date = fields.Date(related="snapshot_id.balance_date", store=True, index=True)
    snapshot_type = fields.Selection(related="snapshot_id.snapshot_type", store=True)
    state = fields.Selection(related="snapshot_id.state", store=True)

    _product_uniq = models.Constraint("unique(snapshot_id, product_id)", "المادة مكررة في نفس الكشف.")

    @api.depends("product_id")
    def _compute_uom(self):
        for line in self:
            if line.product_id and not line.uom_id:
                line.uom_id = line.product_id.uom_id

    @api.depends("quantity", "uom_id", "product_id")
    def _compute_qty(self):
        for line in self:
            qty = line.quantity
            base = line.product_id.uom_id
            if line.uom_id and base and line.uom_id != base and line.uom_id._has_common_reference(base):
                qty = line.uom_id._compute_quantity(line.quantity, base, round=False)
            line.qty_product_uom = qty

    def _check_convertible(self):
        for line in self:
            base = line.product_id.uom_id
            if line.uom_id != base and not line.uom_id._has_common_reference(base):
                raise UserError("الوحدة %s غير متوافقة مع وحدة المادة %s." % (line.uom_id.name, line.product_id.display_name))
