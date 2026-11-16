from datetime import datetime, time

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError, AccessError

RIYADH = pytz.timezone("Asia/Riyadh")

G_EMPLOYEE = "alwabrn_warehouse.group_alw_employee"
G_SUPERVISOR = "alwabrn_warehouse.group_alw_supervisor"
G_AUDITOR = "alwabrn_warehouse.group_alw_auditor"
G_MANAGER = "alwabrn_warehouse.group_alw_manager"
G_ADMIN = "alwabrn_warehouse.group_alw_admin"


def riyadh_day_end_utc(day):
    """End of the given Riyadh calendar day as naive UTC datetime."""
    local = RIYADH.localize(datetime.combine(day, time(23, 59, 59)))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def riyadh_day_start_utc(day):
    local = RIYADH.localize(datetime.combine(day, time(0, 0, 0)))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def riyadh_today():
    return datetime.now(RIYADH).date()


class AlwabrnStockService(models.AbstractModel):
    """Helpers on top of the native Odoo stock engine (no parallel ledger)."""
    _name = "alwabrn.stock.service"
    _description = "Al Wabran stock helpers"

    @api.model
    def require_manager(self):
        if not (self.env.su or self.env.user.has_group(G_MANAGER)):
            raise AccessError("هذه العملية تتطلب صلاحية مدير المستودع.")

    @api.model
    def warehouse(self):
        wh = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.env.company.id)], order="id", limit=1)
        if not wh:
            raise UserError("لا يوجد مستودع معرف للشركة.")
        return wh

    @api.model
    def role_location(self, role):
        loc = self.env["stock.location"].sudo().search(
            [("alw_role", "=", role), ("company_id", "in", [self.env.company.id, False])],
            order="id", limit=1)
        if not loc:
            raise UserError("لم يتم العثور على موقع مخزني بالدور: %s" % role)
        return loc

    @api.model
    def main_stock_location(self):
        return self.warehouse().lot_stock_id

    @api.model
    def balance_at(self, location, cutoff=None, products=None):
        """Quantity per product (in product UoM) inside ``location`` (children
        included) considering only done moves up to ``cutoff`` (naive UTC).
        Computed from stock.move.line = the single authoritative ledger."""
        MoveLine = self.env["stock.move.line"].sudo()
        base = [("state", "=", "done")]
        if cutoff:
            base.append(("date", "<=", cutoff))
        if products is not None:
            base.append(("product_id", "in", products.ids))
        inside = ("parent_path", "=like", location.parent_path + "%")
        res = {}
        dom_in = base + [("location_dest_id.parent_path", "=like", location.parent_path + "%"),
                         "!", ("location_id.parent_path", "=like", location.parent_path + "%")]
        dom_out = base + [("location_id.parent_path", "=like", location.parent_path + "%"),
                          "!", ("location_dest_id.parent_path", "=like", location.parent_path + "%")]
        for product, qty in MoveLine._read_group(dom_in, ["product_id"], ["quantity_product_uom:sum"]):
            res[product.id] = res.get(product.id, 0.0) + qty
        for product, qty in MoveLine._read_group(dom_out, ["product_id"], ["quantity_product_uom:sum"]):
            res[product.id] = res.get(product.id, 0.0) - qty
        return res

    @api.model
    def current_qty(self, product, location):
        quants = self.env["stock.quant"].sudo().search(
            [("product_id", "=", product.id), ("location_id", "child_of", location.id)])
        return sum(quants.mapped("quantity"))

    @api.model
    def convert_qty(self, qty, from_uom, product):
        """Convert only through an approved (common reference) conversion."""
        to_uom = product.uom_id
        if not from_uom or from_uom == to_uom:
            return qty
        if not from_uom._has_common_reference(to_uom):
            raise UserError("لا يوجد تحويل معتمد من الوحدة %s إلى %s للمادة %s." % (
                from_uom.name, to_uom.name, product.display_name))
        return from_uom._compute_quantity(qty, to_uom, round=False)

    @api.model
    def uom_from_text(self, text):
        from ..tools.text_parser import detect_unit
        code = (text or "").strip().upper()
        if code not in ("EA", "KM", "M", "KIT", "R", "DRUM"):
            code = detect_unit(text)
        if not code:
            return self.env["uom.uom"]
        return self.env["uom.uom"].sudo().search([("alw_code", "=", code), ("active", "=", True)], limit=1)


class UomUom(models.Model):
    _inherit = "uom.uom"

    alw_code = fields.Char("رمز الوحدة بكتالوج SEC", index=True, copy=False)
