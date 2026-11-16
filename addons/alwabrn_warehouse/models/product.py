from odoo import api, fields, models
from odoo.exceptions import ValidationError


class StockLocation(models.Model):
    _inherit = "stock.location"

    alw_role = fields.Selection([
        ("warehouse", "المستودع الرئيسي"),
        ("custody_root", "جذر عهد المواقع"),
        ("custody", "عهدة موقع"),
        ("consumption", "استهلاك المشاريع"),
        ("damaged", "مواد تالفة"),
        ("scrap", "سكراب"),
        ("owner", "مالك المواد / المورد (SEC)"),
    ], string="دور الموقع بنظام الوبران", index=True, copy=False)
    alw_site_id = fields.Many2one("alwabrn.site", string="موقع المشروع", copy=False, index="btree_not_null")


class ProductTemplate(models.Model):
    _inherit = "product.template"

    alw_sec_code = fields.Char("كود SEC", index=True, copy=False, tracking=True)
    alw_is_material = fields.Boolean("مادة عقد موحد", index=True, default=False)
    alw_name_ar = fields.Char("الوصف بالعربية")
    alw_reference_price = fields.Float("السعر المرجعي", digits="Product Price")
    alw_needs_verification = fields.Boolean("يحتاج تحقق", tracking=True)
    alw_verification_note = fields.Text("ملاحظة التحقق")
    alw_source_row = fields.Integer("رقم الصف بالملف المصدر", readonly=True)
    alw_source_formula = fields.Char("صيغة العمود G بالمصدر", readonly=True)
    alw_qty_warehouse = fields.Float("رصيد المستودع", compute="_compute_alw_quantities", digits="Product Unit")
    alw_qty_custody = fields.Float("عهد المواقع", compute="_compute_alw_quantities", digits="Product Unit")
    alw_qty_damaged = fields.Float("تالف", compute="_compute_alw_quantities", digits="Product Unit")
    alw_qty_total = fields.Float("إجمالي عهدة الشركة", compute="_compute_alw_quantities", digits="Product Unit")
    alw_last_reconciliation = fields.Date("آخر مطابقة", compute="_compute_alw_recon")
    alw_open_discrepancies = fields.Integer("فروقات مفتوحة", compute="_compute_alw_recon")

    _alw_sec_code_uniq = models.UniqueIndex(
        "(alw_sec_code) WHERE alw_sec_code IS NOT NULL AND active",
        "كود SEC يجب ألا يتكرر.")

    @api.constrains("alw_sec_code")
    def _check_alw_sec_code(self):
        for rec in self:
            if rec.alw_sec_code and rec.alw_sec_code != rec.alw_sec_code.strip():
                raise ValidationError("كود SEC يحتوي على مسافات زائدة.")

    def _compute_alw_quantities(self):
        Quant = self.env["stock.quant"].sudo()
        data = Quant._read_group(
            [("product_id.product_tmpl_id", "in", self.ids), ("location_id.usage", "=", "internal")],
            ["product_id", "location_id"], ["quantity:sum"])
        acc = {}
        for product, location, qty in data:
            role = location.alw_role or "warehouse"
            key = "custody" if role == "custody" else "damaged" if role == "damaged" else "warehouse"
            d = acc.setdefault(product.product_tmpl_id.id, {"warehouse": 0.0, "custody": 0.0, "damaged": 0.0})
            d[key] += qty
        for rec in self:
            d = acc.get(rec.id, {})
            rec.alw_qty_warehouse = d.get("warehouse", 0.0)
            rec.alw_qty_custody = d.get("custody", 0.0)
            rec.alw_qty_damaged = d.get("damaged", 0.0)
            rec.alw_qty_total = rec.alw_qty_warehouse + rec.alw_qty_custody + rec.alw_qty_damaged

    def _compute_alw_recon(self):
        Line = self.env["alwabrn.reconciliation.line"].sudo()
        for rec in self:
            last = Line.search([("product_id.product_tmpl_id", "=", rec.id),
                                ("reconciliation_id.state", "!=", "draft")],
                               order="date desc, id desc", limit=1)
            rec.alw_last_reconciliation = last.date
            rec.alw_open_discrepancies = Line.search_count([
                ("product_id.product_tmpl_id", "=", rec.id), ("status", "=", "diff"),
                ("resolution", "=", "open")])

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("alw_sec_code") and not vals.get("default_code"):
                vals["default_code"] = vals["alw_sec_code"]
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        if "alw_sec_code" in vals:
            for rec in self:
                if rec.alw_sec_code and rec.default_code != rec.alw_sec_code:
                    rec.default_code = rec.alw_sec_code
        return res

    @api.model
    def alw_find_by_sec(self, code):
        code = (code or "").strip()
        if not code:
            return self.env["product.product"]
        tmpl = self.sudo().search([("alw_sec_code", "=", code)], limit=1)
        return tmpl.product_variant_id

    def action_alw_stock_card(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": "كرت الصنف - %s" % (self.alw_sec_code or self.name),
            "res_model": "alwabrn.stock.card.line", "view_mode": "list,pivot",
            "domain": [("product_tmpl_id", "=", self.id)],
        }

    def action_alw_custody(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": "عهد المواقع - %s" % (self.alw_sec_code or self.name),
            "res_model": "alwabrn.custody.line", "view_mode": "list,pivot",
            "domain": [("product_tmpl_id", "=", self.id)],
        }

    def action_alw_discrepancies(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": "الفروقات",
            "res_model": "alwabrn.reconciliation.line", "view_mode": "list",
            "domain": [("product_id.product_tmpl_id", "=", self.id), ("status", "=", "diff")],
        }
