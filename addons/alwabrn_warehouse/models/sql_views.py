from odoo import fields, models, tools



class AlwabrnSqlViewMixin(models.AbstractModel):
    """SQL views read native stock tables: flush pending ORM writes first."""
    _name = "alwabrn.sql.view.mixin"
    _description = "SQL view helper"

    def _search(self, *args, **kwargs):
        self.env.flush_all()
        return super()._search(*args, **kwargs)

    def _read_group(self, *args, **kwargs):
        self.env.flush_all()
        return super()._read_group(*args, **kwargs)


ROLE_SEL = [("warehouse", "المستودع"), ("custody", "عهدة موقع"), ("damaged", "تالف")]


class AlwabrnStockBalance(models.Model):
    """Read-only view over native stock.quant (current official balances)."""
    _name = "alwabrn.stock.balance"
    _inherit = "alwabrn.sql.view.mixin"
    _description = "الرصيد الحالي"
    _auto = False
    _order = "sec_code"

    product_id = fields.Many2one("product.product", "المادة", readonly=True)
    product_tmpl_id = fields.Many2one("product.template", "بطاقة المادة", readonly=True)
    sec_code = fields.Char("كود SEC", readonly=True)
    categ_id = fields.Many2one("product.category", "الفئة", readonly=True)
    location_id = fields.Many2one("stock.location", "الموقع المخزني", readonly=True)
    role = fields.Selection(ROLE_SEL, "نوع الرصيد", readonly=True)
    site_id = fields.Many2one("alwabrn.site", "موقع المشروع", readonly=True)
    uom_id = fields.Many2one("uom.uom", "الوحدة", readonly=True)
    quantity = fields.Float("الكمية", readonly=True, digits="Product Unit")
    reference_price = fields.Float("السعر المرجعي", readonly=True, aggregator="avg")
    value = fields.Float("القيمة المرجعية", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW alwabrn_stock_balance AS
            SELECT min(q.id) AS id, q.product_id, pp.product_tmpl_id, pt.alw_sec_code AS sec_code,
                   pt.categ_id, q.location_id,
                   CASE WHEN l.alw_role IN ('custody','damaged') THEN l.alw_role ELSE 'warehouse' END AS role,
                   l.alw_site_id AS site_id, pt.uom_id, sum(q.quantity) AS quantity,
                   max(pt.alw_reference_price) AS reference_price,
                   sum(q.quantity) * max(pt.alw_reference_price) AS value
            FROM stock_quant q
            JOIN stock_location l ON l.id = q.location_id AND l.usage = 'internal'
            JOIN product_product pp ON pp.id = q.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            GROUP BY q.product_id, pp.product_tmpl_id, pt.alw_sec_code, pt.categ_id, q.location_id,
                     l.alw_role, l.alw_site_id, pt.uom_id
            HAVING sum(q.quantity) <> 0
        """)


class AlwabrnCustodyLine(models.Model):
    """Site custody per site/material derived from done stock.move.line."""
    _name = "alwabrn.custody.line"
    _inherit = "alwabrn.sql.view.mixin"
    _description = "عهدة موقع"
    _auto = False
    _order = "site_id, sec_code"

    site_id = fields.Many2one("alwabrn.site", "الموقع", readonly=True)
    project_id = fields.Many2one("alwabrn.project", "المشروع", readonly=True)
    work_order = fields.Char("أمر العمل", readonly=True)
    supervisor_id = fields.Many2one("res.users", "المشرف", readonly=True)
    employee_id = fields.Many2one("res.partner", "الموظف", readonly=True)
    product_id = fields.Many2one("product.product", "المادة", readonly=True)
    product_tmpl_id = fields.Many2one("product.template", "بطاقة المادة", readonly=True)
    sec_code = fields.Char("كود SEC", readonly=True)
    uom_id = fields.Many2one("uom.uom", "الوحدة", readonly=True)
    qty_issued = fields.Float("المصروف للموقع", readonly=True, digits="Product Unit")
    qty_returned = fields.Float("المرتجع", readonly=True, digits="Product Unit")
    qty_consumed = fields.Float("المستهلك", readonly=True, digits="Product Unit")
    qty_other_out = fields.Float("تالف / سكراب / أخرى", readonly=True, digits="Product Unit")
    qty_outstanding = fields.Float("العهدة القائمة", readonly=True, digits="Product Unit")

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW alwabrn_custody_line AS
            SELECT min(x.ml_id) AS id, x.site_id, s.project_id, s.work_order, s.supervisor_id, s.employee_id,
                   x.product_id, pp.product_tmpl_id, pt.alw_sec_code AS sec_code, pt.uom_id,
                   sum(x.issued) AS qty_issued, sum(x.returned) AS qty_returned,
                   sum(x.consumed) AS qty_consumed, sum(x.other_out) AS qty_other_out,
                   sum(x.issued) - sum(x.returned) - sum(x.consumed) - sum(x.other_out) AS qty_outstanding
            FROM (
                SELECT ml.id AS ml_id, ld.alw_site_id AS site_id, ml.product_id,
                       ml.quantity_product_uom AS issued, 0 AS returned, 0 AS consumed, 0 AS other_out
                FROM stock_move_line ml
                JOIN stock_location ld ON ld.id = ml.location_dest_id AND ld.alw_role = 'custody'
                WHERE ml.state = 'done' AND ml.location_id <> ml.location_dest_id
                UNION ALL
                SELECT ml.id, ls.alw_site_id, ml.product_id, 0,
                       CASE WHEN ld.usage = 'internal' AND coalesce(ld.alw_role,'') NOT IN ('damaged')
                            THEN ml.quantity_product_uom ELSE 0 END,
                       CASE WHEN ld.alw_role = 'consumption' THEN ml.quantity_product_uom ELSE 0 END,
                       CASE WHEN (ld.usage <> 'internal' AND coalesce(ld.alw_role,'') <> 'consumption')
                                 OR ld.alw_role = 'damaged' THEN ml.quantity_product_uom ELSE 0 END
                FROM stock_move_line ml
                JOIN stock_location ls ON ls.id = ml.location_id AND ls.alw_role = 'custody'
                JOIN stock_location ld ON ld.id = ml.location_dest_id
                WHERE ml.state = 'done' AND ml.location_id <> ml.location_dest_id
            ) x
            JOIN alwabrn_site s ON s.id = x.site_id
            JOIN product_product pp ON pp.id = x.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            GROUP BY x.site_id, s.project_id, s.work_order, s.supervisor_id, s.employee_id,
                     x.product_id, pp.product_tmpl_id, pt.alw_sec_code, pt.uom_id
        """)


class AlwabrnStockCardLine(models.Model):
    """Material stock card: every done native move line, with running balances."""
    _name = "alwabrn.stock.card.line"
    _inherit = "alwabrn.sql.view.mixin"
    _description = "كرت الصنف"
    _auto = False
    _order = "date, id"

    date = fields.Datetime("التاريخ", readonly=True)
    product_id = fields.Many2one("product.product", "المادة", readonly=True)
    product_tmpl_id = fields.Many2one("product.template", "بطاقة المادة", readonly=True)
    sec_code = fields.Char("كود SEC", readonly=True)
    categ_id = fields.Many2one("product.category", "الفئة", readonly=True)
    reference = fields.Char("مرجع Odoo", readonly=True)
    transaction_id = fields.Many2one("alwabrn.transaction", "حركة الوبران", readonly=True)
    tx_type = fields.Selection(lambda s: s.env["alwabrn.transaction"]._fields["tx_type"].selection,
                               string="نوع الحركة", readonly=True)
    site_id = fields.Many2one("alwabrn.site", "الموقع", readonly=True)
    project_id = fields.Many2one("alwabrn.project", "المشروع", readonly=True)
    document_ref = fields.Char("مرجع المستند", readonly=True)
    location_id = fields.Many2one("stock.location", "من", readonly=True)
    location_dest_id = fields.Many2one("stock.location", "إلى", readonly=True)
    uom_id = fields.Many2one("uom.uom", "الوحدة", readonly=True)
    quantity = fields.Float("الكمية", readonly=True, digits="Product Unit")
    delta_warehouse = fields.Float("أثر على المستودع", readonly=True, digits="Product Unit")
    delta_custody = fields.Float("أثر على العهد", readonly=True, digits="Product Unit")
    delta_company = fields.Float("أثر على عهدة الشركة", readonly=True, digits="Product Unit")
    balance_warehouse = fields.Float("رصيد المستودع بعد الحركة", readonly=True, digits="Product Unit", aggregator=None)
    balance_company = fields.Float("رصيد الشركة بعد الحركة", readonly=True, digits="Product Unit", aggregator=None)

    def action_open_transaction(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.transaction",
                "res_id": self.transaction_id.id, "view_mode": "form"}

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW alwabrn_stock_card_line AS
            WITH base AS (
                SELECT ml.id, ml.date, ml.product_id, pp.product_tmpl_id, pt.alw_sec_code AS sec_code,
                       pt.categ_id, m.reference, rel.alwabrn_transaction_id AS transaction_id,
                       ml.location_id, ml.location_dest_id, pt.uom_id, ml.quantity_product_uom AS quantity,
                       (CASE WHEN ld.usage='internal' AND coalesce(ld.alw_role,'warehouse')='warehouse' THEN 1 ELSE 0 END
                        - CASE WHEN ls.usage='internal' AND coalesce(ls.alw_role,'warehouse')='warehouse' THEN 1 ELSE 0 END
                       ) * ml.quantity_product_uom AS delta_warehouse,
                       (CASE WHEN ld.alw_role='custody' THEN 1 ELSE 0 END
                        - CASE WHEN ls.alw_role='custody' THEN 1 ELSE 0 END) * ml.quantity_product_uom AS delta_custody,
                       (CASE WHEN ld.usage='internal' THEN 1 ELSE 0 END
                        - CASE WHEN ls.usage='internal' THEN 1 ELSE 0 END) * ml.quantity_product_uom AS delta_company
                FROM stock_move_line ml
                JOIN stock_move m ON m.id = ml.move_id
                JOIN stock_location ls ON ls.id = ml.location_id
                JOIN stock_location ld ON ld.id = ml.location_dest_id
                JOIN product_product pp ON pp.id = ml.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                LEFT JOIN alwabrn_transaction_stock_move_rel rel ON rel.stock_move_id = m.id
                WHERE ml.state = 'done'
            )
            SELECT b.*, t.tx_type, t.site_id, t.project_id, t.document_ref,
                   sum(b.delta_warehouse) OVER (PARTITION BY b.product_id ORDER BY b.date, b.id) AS balance_warehouse,
                   sum(b.delta_company) OVER (PARTITION BY b.product_id ORDER BY b.date, b.id) AS balance_company
            FROM base b LEFT JOIN alwabrn_transaction t ON t.id = b.transaction_id
        """)
