import base64
import io

import xlsxwriter

from odoo.tests.common import TransactionCase, new_test_user


class AlwCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        if not cls.env["product.template"].search_count([("alw_is_material", "=", True)]):
            # The confidential workbook is not in the public repository: point
            # ALWABRN_CATALOG_FILE to it (or place it in data/) to run the suite.
            import os
            import unittest
            path = os.environ.get("ALWABRN_CATALOG_FILE")
            if not path or not os.path.exists(path):
                raise unittest.SkipTest("material catalog not loaded (set ALWABRN_CATALOG_FILE)")
            from ..hooks import import_catalog
            with open(path, "rb") as f:
                import_catalog(cls.env, f.read())
        cls.manager = new_test_user(cls.env, "alw_mgr", groups="alwabrn_warehouse.group_alw_manager")
        cls.employee = new_test_user(cls.env, "alw_emp", groups="alwabrn_warehouse.group_alw_employee")
        cls.auditor = new_test_user(cls.env, "alw_aud", groups="alwabrn_warehouse.group_alw_auditor")
        cls.supervisor = new_test_user(cls.env, "alw_sup", groups="alwabrn_warehouse.group_alw_supervisor")
        cls.svc = cls.env["alwabrn.stock.service"]
        cls.wh = cls.svc.main_stock_location()
        cls.Tx = cls.env["alwabrn.transaction"]
        cls.p1 = cls.env["product.template"].alw_find_by_sec("908111001")  # KM
        cls.p2 = cls.env["product.template"].alw_find_by_sec("908111002")  # KM
        cls.ea = cls.env["product.template"].search(
            [("alw_is_material", "=", True), ("uom_id", "=", cls.env.ref("uom.product_uom_unit").id)], limit=1
        ).product_variant_id
        cls.site = cls.env["alwabrn.site"].create({"name": "موقع اختبار", "code": "105",
                                                   "supervisor_id": cls.supervisor.id})
        cls.site2 = cls.env["alwabrn.site"].create({"name": "موقع آخر", "code": "106"})
        cls.m = cls.env.ref("uom.product_uom_meter")
        cls.km = cls.env.ref("uom.product_uom_km")

    def tx(self, tx_type, product, qty, site=None, uom=None, user=None, post=True, **kw):
        vals = dict(tx_type=tx_type, site_id=site.id if site else False,
                    line_ids=[(0, 0, {"product_id": product.id, "quantity": qty,
                                      "uom_id": (uom or product.uom_id).id})], **kw)
        tx = self.Tx.with_user(user or self.employee).create(vals)
        if post:
            tx.with_user(self.employee).action_submit()
            tx.with_user(self.manager).action_approve()
        return tx

    def qty(self, product, location=None):
        return self.svc.current_qty(product, location or self.wh)

    def company_qty(self, product):
        quants = self.env["stock.quant"].search([("product_id", "=", product.id),
                                                 ("location_id.usage", "=", "internal")])
        return sum(quants.mapped("quantity"))

    def base_scenario(self, product=None):
        p = product or self.ea
        self.tx("opening", p, 100)
        self.tx("receipt", p, 30)
        self.tx("issue_site", p, 20, site=self.site)
        self.tx("return_in", p, 5, site=self.site)
        return p

    def xlsx(self, rows, header=("SEC #", "Qty", "Unit")):
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet()
        for c, h in enumerate(header):
            ws.write(0, c, h)
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row):
                ws.write(r, c, v)
        wb.close()
        return buf.getvalue()

    def png(self, text="x", seed=0):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (900, 200), "white")
        ImageDraw.Draw(img).text((20 + seed, 80), text, fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue())
