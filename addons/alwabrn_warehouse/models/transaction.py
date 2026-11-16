from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

from .common import G_MANAGER

TX_TYPES = [
    ("opening", "رصيد افتتاحي"),
    ("receipt", "استلام مواد (وارد)"),
    ("issue_site", "صرف إلى عهدة موقع"),
    ("issue", "صرف مباشر (استهلاك من المستودع)"),
    ("transfer", "تحويل داخلي"),
    ("return_in", "ارتجاع من موقع"),
    ("return_legacy", "ارتجاع من أمر عمل (صُرف قبل تشغيل النظام)"),
    ("consume", "استهلاك بالموقع"),
    ("return_out", "إرجاع للمالك / المورد"),
    ("damaged", "مواد تالفة"),
    ("scrap", "سكراب"),
    ("adj_plus", "تسوية بالزيادة"),
    ("adj_minus", "تسوية بالنقص"),
]
INVENTORY_TYPES = ("opening", "adj_plus", "adj_minus")


class AlwabrnTransaction(models.Model):
    _name = "alwabrn.transaction"
    _description = "حركة مواد"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char("رقم الحركة", readonly=True, copy=False, default="جديد", index=True)
    tx_type = fields.Selection(TX_TYPES, "نوع الحركة", required=True, tracking=True, index=True)
    date = fields.Datetime("تاريخ ووقت الحركة", required=True, default=fields.Datetime.now, tracking=True, index=True)
    state = fields.Selection([
        ("draft", "مسودة"), ("pending", "بانتظار الاعتماد"), ("posted", "مرحّلة"),
        ("rejected", "مرفوضة"), ("reversed", "معكوسة"),
    ], "الحالة", default="draft", required=True, tracking=True, index=True, copy=False)
    site_id = fields.Many2one("alwabrn.site", "الموقع", tracking=True, index=True)
    project_id = fields.Many2one("alwabrn.project", "المشروع", compute="_compute_project", store=True, readonly=False)
    work_order = fields.Char("أمر العمل", compute="_compute_project", store=True, readonly=False)
    location_src_id = fields.Many2one("stock.location", "من موقع", compute="_compute_locations", store=True, readonly=False)
    location_dest_id = fields.Many2one("stock.location", "إلى موقع", compute="_compute_locations", store=True, readonly=False)
    document_ref = fields.Char("مرجع المستند", tracking=True, index=True)
    employee_id = fields.Many2one("res.users", "الموظف", default=lambda s: s.env.user, readonly=True, copy=False)
    approver_id = fields.Many2one("res.users", "المعتمد", readonly=True, copy=False)
    approved_on = fields.Datetime("وقت الاعتماد", readonly=True, copy=False)
    note = fields.Text("ملاحظات")
    reason = fields.Text("سبب التسوية / العكس")
    line_ids = fields.One2many("alwabrn.transaction.line", "transaction_id", "الأصناف", copy=True)
    picking_id = fields.Many2one("stock.picking", "مستند المخزون (Odoo)", readonly=True, copy=False)
    move_ids = fields.Many2many("stock.move", string="حركات المخزون", readonly=True, copy=False)
    batch_id = fields.Many2one("alwabrn.import.batch", "دفعة الإدخال", readonly=True, copy=False, index="btree_not_null")
    source_document_ids = fields.Many2many("alwabrn.source.document", string="المستندات المصدر")
    reversal_of_id = fields.Many2one("alwabrn.transaction", "عكس للحركة", readonly=True, copy=False)
    reversed_by_id = fields.Many2one("alwabrn.transaction", "معكوسة بالحركة", readonly=True, copy=False)
    reconciliation_line_id = fields.Many2one("alwabrn.reconciliation.line", "بند المطابقة", readonly=True, copy=False)
    idempotency_key = fields.Char("مفتاح منع التكرار", readonly=True, copy=False, index=True)
    possible_duplicate_ids = fields.Many2many(
        "alwabrn.transaction", "alwabrn_tx_dup_rel", "tx_id", "dup_id",
        string="حركات مشابهة محتملة", compute="_compute_possible_duplicates")
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)
    line_count = fields.Integer("عدد الأصناف", compute="_compute_line_count", store=True)

    _idempotency_uniq = models.UniqueIndex(
        "(idempotency_key) WHERE idempotency_key IS NOT NULL", "هذه الحركة مسجلة مسبقاً (مفتاح منع التكرار).")

    @api.depends("line_ids")
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends("site_id")
    def _compute_project(self):
        for rec in self:
            if rec.site_id:
                rec.project_id = rec.site_id.project_id
                rec.work_order = rec.work_order or rec.site_id.work_order

    @api.depends("tx_type", "site_id")
    def _compute_locations(self):
        svc = self.env["alwabrn.stock.service"]
        for rec in self:
            if rec.state != "draft" and rec.location_src_id and rec.location_dest_id:
                continue
            src, dest = rec._default_locations(svc)
            rec.location_src_id, rec.location_dest_id = src, dest

    def _default_locations(self, svc):
        self.ensure_one()
        Loc = self.env["stock.location"]
        t = self.tx_type
        if not t:
            return Loc, Loc
        wh = svc.main_stock_location()
        site = self.site_id.location_id
        owner = svc.role_location("owner")
        inv_loss = self.env["stock.location"].sudo().search(
            [("usage", "=", "inventory"), ("alw_role", "=", False),
             ("company_id", "in", [self.env.company.id, False])], limit=1)
        mapping = {
            "opening": (inv_loss, wh), "adj_plus": (inv_loss, self.location_dest_id or wh),
            "adj_minus": (self.location_src_id or wh, inv_loss),
            "receipt": (owner, wh), "issue_site": (wh, site), "return_in": (site, wh),
            "return_legacy": (svc.role_location("consumption"), wh),
            "issue": (wh, svc.role_location("consumption")),
            "consume": (site, svc.role_location("consumption")),
            "return_out": (wh, owner),
            "damaged": (site or wh, svc.role_location("damaged")),
            "scrap": (site or wh, svc.role_location("scrap")),
            "transfer": (self.location_src_id or wh, self.location_dest_id or Loc),
        }
        return mapping[t]

    def _compute_possible_duplicates(self):
        for rec in self:
            dups = self.browse()
            if rec.line_ids and rec.state in ("draft", "pending"):
                day = fields.Date.to_date(rec.date)
                cands = self.search([
                    ("id", "!=", rec.id or 0), ("tx_type", "=", rec.tx_type),
                    ("state", "in", ("pending", "posted")), ("site_id", "=", rec.site_id.id),
                    ("line_ids.product_id", "in", rec.line_ids.product_id.ids)], limit=50)
                sig = rec._signature()
                dups = cands.filtered(lambda c: fields.Date.to_date(c.date) == day and c._signature() & sig)
            rec.possible_duplicate_ids = dups

    def _signature(self):
        return {(l.product_id.id, round(l.qty_product_uom, 4)) for l in self.line_ids}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "جديد") == "جديد":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("alwabrn.transaction") or "جديد"
        return super().create(vals_list)

    def write(self, vals):
        locked = {"tx_type", "date", "site_id", "location_src_id", "location_dest_id", "line_ids"}
        if locked & set(vals) and not self.env.context.get("alw_internal"):
            if any(rec.state in ("posted", "reversed") for rec in self):
                raise UserError("لا يمكن تعديل حركة مرحّلة. استخدم إجراء «عكس الحركة».")
        return super().write(vals)

    def unlink(self):
        if any(rec.state != "draft" for rec in self):
            raise UserError("لا يمكن حذف حركة غير مسودة. الحركات المرحّلة تُعكس ولا تُحذف.")
        return super().unlink()

    # ------------------------------------------------------------ workflow
    def _check_ready(self):
        for rec in self:
            if not rec.line_ids:
                raise UserError("أضف صنفاً واحداً على الأقل.")
            if rec.tx_type in ("issue_site", "return_in", "consume") and not rec.site_id:
                raise UserError("حدد الموقع لهذه الحركة.")
            if not rec.location_src_id or not rec.location_dest_id:
                raise UserError("حدد موقع المصدر والوجهة.")
            if rec.location_src_id == rec.location_dest_id:
                raise UserError("موقع المصدر والوجهة متطابقان.")
            if rec.tx_type in ("adj_plus", "adj_minus") and not rec.reason:
                raise UserError("سبب التسوية إلزامي.")
            for line in rec.line_ids:
                if float_compare(line.quantity, 0.0, precision_digits=6) <= 0:
                    raise UserError("الكمية يجب أن تكون أكبر من صفر (%s)." % line.product_id.display_name)
                line._check_uom()

    def action_submit(self):
        self._check_ready()
        for rec in self.filtered(lambda r: r.state == "draft"):
            rec.state = "pending"
            self.env["alwabrn.approval"].log(rec, "submit")
        return True

    def action_approve(self):
        """Manager approval -> post through the native Odoo stock engine."""
        self.env["alwabrn.stock.service"].require_manager()
        self._check_ready()
        for rec in self:
            rec._post()
        return True

    def action_reject(self):
        self.env["alwabrn.stock.service"].require_manager()
        for rec in self.filtered(lambda r: r.state in ("draft", "pending")):
            rec.state = "rejected"
            self.env["alwabrn.approval"].log(rec, "reject", rec.reason)
        return True

    def action_reset_draft(self):
        self.filtered(lambda r: r.state == "rejected").write({"state": "draft"})
        return True

    def _check_availability(self):
        """Negative-stock control: an internal source may never go below zero."""
        self.ensure_one()
        if self.location_src_id.usage != "internal":
            return
        svc = self.env["alwabrn.stock.service"]
        need = {}
        for line in self.line_ids:
            need[line.product_id] = need.get(line.product_id, 0.0) + line.qty_product_uom
        for product, qty in need.items():
            available = svc.current_qty(product, self.location_src_id)
            if float_compare(available, qty, precision_rounding=product.uom_id.rounding) < 0:
                raise UserError(
                    "الرصيد غير كافٍ للمادة %s في «%s»: المتاح %s والمطلوب %s. لا يسمح بالرصيد السالب." % (
                        product.display_name, self.location_src_id.display_name, available, qty))

    def _post(self):
        self.ensure_one()
        # row lock => concurrent double click / retry cannot post twice
        self.flush_recordset()
        self.env.cr.execute("SELECT state FROM alwabrn_transaction WHERE id = %s FOR UPDATE", [self.id])
        state = self.env.cr.fetchone()[0]
        if state == "posted":
            return True  # idempotent
        if state not in ("draft", "pending"):
            raise UserError("لا يمكن ترحيل حركة بالحالة الحالية.")
        self.invalidate_recordset(["state"])
        self._check_availability()
        if self.tx_type in INVENTORY_TYPES:
            moves = self._post_inventory()
            picking = self.env["stock.picking"]
        else:
            picking = self._post_picking()
            moves = picking.move_ids
        # effective (possibly back-dated) date on the native moves
        moves.sudo().write({"date": self.date})
        moves.sudo().move_line_ids.write({"date": self.date})
        if picking:
            picking.sudo().write({"date_done": self.date})
        self.with_context(alw_internal=True).write({
            "state": "posted", "picking_id": picking.id, "move_ids": [(6, 0, moves.ids)],
            "approver_id": self.env.user.id, "approved_on": fields.Datetime.now(),
        })
        self.env["alwabrn.approval"].log(self, "post", self.reason, {
            "type": self.tx_type, "picking": picking.name if picking else None,
            "lines": [{"sec": l.product_id.alw_sec_code, "qty": l.quantity, "uom": l.uom_id.name,
                       "qty_base": l.qty_product_uom} for l in self.line_ids]})
        return True

    def _picking_type(self):
        wh = self.env["alwabrn.stock.service"].warehouse()
        src, dest = self.location_src_id.usage, self.location_dest_id.usage
        if src != "internal" and dest == "internal":
            return wh.in_type_id
        if src == "internal" and dest == "internal":
            return wh.int_type_id
        return wh.out_type_id

    def _post_picking(self):
        Picking = self.env["stock.picking"].sudo()
        picking = Picking.create({
            "picking_type_id": self._picking_type().id,
            "location_id": self.location_src_id.id, "location_dest_id": self.location_dest_id.id,
            "origin": self.name, "scheduled_date": self.date, "company_id": self.company_id.id,
            "move_ids": [(0, 0, {
                "product_id": l.product_id.id, "product_uom_qty": l.quantity, "product_uom": l.uom_id.id,
                "location_id": self.location_src_id.id, "location_dest_id": self.location_dest_id.id,
                "origin": self.name, "company_id": self.company_id.id,
            }) for l in self.line_ids],
        })
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        res = picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True,
                                   cancel_backorder=True).button_validate()
        if picking.state != "done":
            raise UserError("تعذر ترحيل مستند المخزون في Odoo: %s" % (res if res is not True else picking.state))
        return picking

    def _post_inventory(self):
        """Opening balance / adjustments through native Odoo inventory adjustment."""
        Quant = self.env["stock.quant"].sudo().with_context(inventory_mode=True, inventory_name=self.name)
        location = self.location_dest_id if self.tx_type in ("opening", "adj_plus") else self.location_src_id
        if location.usage != "internal":
            raise UserError("التسوية الجردية تتم على موقع داخلي فقط.")
        Move = self.env["stock.move"].sudo()
        before = Move.search([("is_inventory", "=", True), ("inventory_name", "=", self.name)])
        for line in self.line_ids:
            current = self.env["alwabrn.stock.service"].current_qty(line.product_id, location)
            if self.tx_type == "opening":
                if float_compare(current, 0.0, precision_rounding=line.product_id.uom_id.rounding) != 0:
                    raise UserError(
                        "يوجد رصيد سابق (%s) للمادة %s؛ الرصيد الافتتاحي يسجل مرة واحدة. استخدم التسوية." % (
                            current, line.product_id.display_name))
                target = line.qty_product_uom
            elif self.tx_type == "adj_plus":
                target = current + line.qty_product_uom
            else:
                target = current - line.qty_product_uom
            line.previous_qty = current
            quant = Quant.search([("product_id", "=", line.product_id.id), ("location_id", "=", location.id)], limit=1)
            if quant:
                own = quant.quantity
                quant.inventory_quantity = own + (target - current)
            else:
                quant = Quant.create({"product_id": line.product_id.id, "location_id": location.id,
                                      "inventory_quantity": target - current})
            quant.action_apply_inventory()
        return Move.search([("is_inventory", "=", True), ("inventory_name", "=", self.name)]) - before

    def action_reverse(self):
        """Controlled reversal: never delete; create and post an opposite movement."""
        self.env["alwabrn.stock.service"].require_manager()
        opposite = {"adj_plus": "adj_minus", "adj_minus": "adj_plus", "opening": "adj_minus"}
        for rec in self:
            if rec.state != "posted":
                raise UserError("يمكن عكس الحركات المرحّلة فقط.")
            if rec.reversed_by_id:
                raise UserError("الحركة معكوسة مسبقاً.")
            rev = self.create({
                "tx_type": opposite.get(rec.tx_type, rec.tx_type),
                "date": fields.Datetime.now(), "site_id": rec.site_id.id,
                "document_ref": rec.document_ref, "reversal_of_id": rec.id,
                "reason": "عكس الحركة %s. %s" % (rec.name, rec.reason or ""),
                "note": "حركة عكسية آلية للحركة %s" % rec.name,
                "idempotency_key": "REV-%s" % rec.id,
                "line_ids": [(0, 0, {"product_id": l.product_id.id, "quantity": l.quantity,
                                     "uom_id": l.uom_id.id}) for l in rec.line_ids],
            })
            rev.write({"location_src_id": rec.location_dest_id.id, "location_dest_id": rec.location_src_id.id})
            rev._check_ready()
            rev._post()
            rec.with_context(alw_internal=True).write({"state": "reversed", "reversed_by_id": rev.id})
            self.env["alwabrn.approval"].log(rec, "reverse", details={"reversal": rev.name})
        return True

    def action_view_picking(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "stock.picking",
                "res_id": self.picking_id.id, "view_mode": "form"}


class AlwabrnTransactionLine(models.Model):
    _name = "alwabrn.transaction.line"
    _description = "بند حركة مواد"
    _order = "transaction_id, sequence, id"

    transaction_id = fields.Many2one("alwabrn.transaction", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    product_id = fields.Many2one("product.product", "المادة", required=True, index=True,
                                 domain=[("alw_is_material", "=", True)])
    sec_code = fields.Char("كود SEC", related="product_id.alw_sec_code", store=True, index=True)
    quantity = fields.Float("الكمية", required=True, digits="Product Unit")
    uom_id = fields.Many2one("uom.uom", "الوحدة", required=True, compute="_compute_uom", store=True,
                             readonly=False, precompute=True)
    product_uom_id = fields.Many2one("uom.uom", "وحدة المادة الأساسية", related="product_id.uom_id")
    qty_product_uom = fields.Float("الكمية بالوحدة الأساسية", compute="_compute_qty_product_uom",
                                   store=True, digits=(16, 6))
    previous_qty = fields.Float("الرصيد قبل التسوية", readonly=True, digits="Product Unit")
    available_qty = fields.Float("المتاح بالمصدر", compute="_compute_available")
    note = fields.Char("ملاحظة")
    tx_type = fields.Selection(related="transaction_id.tx_type", store=True)
    state = fields.Selection(related="transaction_id.state", store=True)
    date = fields.Datetime(related="transaction_id.date", store=True, index=True)
    site_id = fields.Many2one(related="transaction_id.site_id", store=True)

    @api.depends("product_id")
    def _compute_uom(self):
        for line in self:
            if line.product_id and not line.uom_id:
                line.uom_id = line.product_id.uom_id

    @api.depends("quantity", "uom_id", "product_id")
    def _compute_qty_product_uom(self):
        for line in self:
            qty = line.quantity
            if line.product_id and line.uom_id and line.uom_id != line.product_id.uom_id \
                    and line.uom_id._has_common_reference(line.product_id.uom_id):
                qty = line.uom_id._compute_quantity(line.quantity, line.product_id.uom_id, round=False)
            line.qty_product_uom = qty

    @api.depends("product_id", "transaction_id.location_src_id")
    def _compute_available(self):
        svc = self.env["alwabrn.stock.service"]
        for line in self:
            src = line.transaction_id.location_src_id
            line.available_qty = svc.current_qty(line.product_id, src) \
                if line.product_id and src and src.usage == "internal" else 0.0

    def _check_uom(self):
        for line in self:
            base = line.product_id.uom_id
            if line.uom_id != base and not line.uom_id._has_common_reference(base):
                raise ValidationError("الوحدة %s غير متوافقة مع وحدة المادة %s (%s). لا يوجد تحويل معتمد." % (
                    line.uom_id.name, line.product_id.display_name, base.name))

    @api.constrains("uom_id", "product_id")
    def _constrain_uom(self):
        self._check_uom()
