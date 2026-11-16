from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import AlwCase


@tagged("post_install", "-at_install", "alwabrn")
class TestInventory(AlwCase):

    def test_00_catalog(self):
        Product = self.env["product.template"]
        self.assertEqual(Product.search_count([("alw_is_material", "=", True)]), 624)
        root = self.env.ref("alwabrn_warehouse.categ_sec_materials")
        self.assertEqual(self.env["product.category"].search_count([("parent_id", "=", root.id)]), 41)
        flagged = Product.search([("alw_needs_verification", "=", True)])
        self.assertEqual(set(flagged.mapped("alw_sec_code")), {"52", "908325002"})
        self.assertEqual(flagged.filtered(lambda p: p.alw_sec_code == "908325002").categ_id, root)
        # Sheet1 reference row must not be imported twice
        self.assertEqual(Product.search_count([("alw_sec_code", "=", "908114004")]), 1)
        # re-import is idempotent (the workbook is not shipped in the public repository)
        from odoo.tools.misc import file_open
        from ..hooks import import_catalog
        try:
            with file_open("alwabrn_warehouse/data/materials_2021.xlsx", "rb") as f:
                content = f.read()
        except FileNotFoundError:
            self.skipTest("materials workbook not present")
        stats = import_catalog(self.env, content)
        self.assertEqual((stats["created"], stats["existing"]), (0, 624))

    def test_01_basic_inventory(self):
        p = self.base_scenario()
        self.assertEqual(self.qty(p), 115)
        self.assertEqual(self.qty(p, self.site.location_id), 15)
        self.assertEqual(self.company_qty(p), 130)
        tmpl = p.product_tmpl_id
        self.assertEqual((tmpl.alw_qty_warehouse, tmpl.alw_qty_custody, tmpl.alw_qty_total), (115, 15, 130))
        cust = self.env["alwabrn.custody.line"].search([("site_id", "=", self.site.id), ("product_id", "=", p.id)])
        self.assertEqual((cust.qty_issued, cust.qty_returned, cust.qty_outstanding), (20, 5, 15))

    def test_02_consumption(self):
        p = self.base_scenario()
        before = self.company_qty(p)
        self.tx("consume", p, 10, site=self.site)
        self.assertEqual(self.qty(p, self.site.location_id), 5)
        self.assertEqual(self.company_qty(p), before - 10)
        self.assertEqual(self.qty(p), 115, "warehouse must not change on site consumption")
        cust = self.env["alwabrn.custody.line"].search([("site_id", "=", self.site.id), ("product_id", "=", p.id)])
        self.assertEqual((cust.qty_consumed, cust.qty_outstanding), (10, 5))

    def _snapshot(self, product, qty, kind="book", uom=None):
        snap = self.env["alwabrn.daily.snapshot"].with_user(self.employee).create({
            "snapshot_type": kind,
            "line_ids": [(0, 0, {"product_id": product.id, "quantity": qty, "uom_id": (uom or product.uom_id).id})]})
        snap.with_user(self.manager).action_approve()
        return snap

    def test_03_reconciliation_book(self):
        p = self.base_scenario()
        snap = self._snapshot(p, 110)
        self.assertEqual(self.qty(p), 115, "a balance statement is not a receipt")
        rec = self.env["alwabrn.reconciliation"].create({"book_snapshot_id": snap.id})
        rec.action_compute()
        line = rec.line_ids
        self.assertEqual((line.odoo_qty, line.book_qty, line.difference, line.status), (115, 110, -5, "diff"))
        self.assertFalse(line.has_physical)
        self.assertEqual(self.qty(p), 115)

    def test_04_physical_count_and_adjustment_approval(self):
        p = self.base_scenario()
        snap = self._snapshot(p, 114, "physical")
        rec = self.env["alwabrn.reconciliation"].create({"physical_snapshot_id": snap.id})
        rec.action_compute()
        line = rec.line_ids
        self.assertEqual((line.difference, line.status), (-1, "diff"))
        self.assertEqual(self.qty(p), 115, "no automatic adjustment")
        with self.assertRaises(UserError):  # reason is mandatory
            line.with_user(self.employee).action_request_adjustment()
        line.note = "نقص مؤكد بالجرد"
        line.with_user(self.employee).action_request_adjustment()
        tx = line.adjustment_tx_id
        self.assertEqual((tx.state, tx.tx_type), ("pending", "adj_minus"))
        self.assertEqual(self.qty(p), 115)
        with self.assertRaises(AccessError):
            tx.with_user(self.employee).action_approve()
        tx.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(p), 114)
        self.assertEqual(line.resolution, "adjusted")
        self.assertTrue(tx.move_ids and all(tx.move_ids.mapped("is_inventory")))
        log = self.env["alwabrn.approval"].search([("action", "=", "adjust")], limit=1)
        self.assertIn("previous_balance", log.details)

    def test_05_missing_data_is_not_zero(self):
        p = self.base_scenario()
        book = self._snapshot(p, 115)
        phys = self._snapshot(self.p1, 3, "physical")
        rec = self.env["alwabrn.reconciliation"].create({"book_snapshot_id": book.id, "physical_snapshot_id": phys.id})
        rec.action_compute()
        l1 = rec.line_ids.filtered(lambda l: l.product_id == p)
        self.assertEqual(l1.status, "match")
        self.assertFalse(l1.has_physical)
        self.assertEqual(l1.diff_physical, 0)

    def test_06_unit_conversion(self):
        self.tx("opening", self.p1, 2)  # KM
        tx = self.tx("issue_site", self.p1, 500, site=self.site, uom=self.m)
        self.assertEqual(tx.line_ids.quantity, 500)
        self.assertEqual(tx.line_ids.uom_id, self.m)
        self.assertAlmostEqual(tx.line_ids.qty_product_uom, 0.5, places=6)
        self.assertAlmostEqual(self.qty(self.p1), 1.5, places=4)
        self.assertAlmostEqual(self.qty(self.p1, self.site.location_id), 0.5, places=4)
        with self.assertRaises(UserError):  # EA cannot be converted to M
            self.tx("receipt", self.ea, 5, uom=self.m, post=False)
        kit = self.env.ref("alwabrn_warehouse.uom_kit")
        self.assertFalse(kit._has_common_reference(self.env.ref("uom.product_uom_unit")))

    def test_07_negative_stock_blocked(self):
        self.tx("opening", self.ea, 10)
        tx = self.tx("issue_site", self.ea, 11, site=self.site, post=False)
        tx.action_submit()
        with self.assertRaises(UserError):
            tx.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 10)

    def test_08_permissions(self):
        tx = self.tx("receipt", self.ea, 5, post=False)
        with self.assertRaises(AccessError):
            tx.with_user(self.employee).action_approve()
        with self.assertRaises(AccessError):
            self.Tx.with_user(self.auditor).create({"tx_type": "receipt"})
        self.assertTrue(self.Tx.with_user(self.auditor).search([("id", "=", tx.id)]))
        # supervisor sees only movements of own sites
        own = self.tx("opening", self.ea, 50) and self.tx("issue_site", self.ea, 5, site=self.site)
        other = self.tx("issue_site", self.ea, 5, site=self.site2)
        visible = self.Tx.with_user(self.supervisor).search([])
        self.assertIn(own, visible)
        self.assertNotIn(other, visible)
        self.assertEqual(self.env["alwabrn.site"].with_user(self.supervisor).search([]), self.site)
        with self.assertRaises(AccessError):
            self.env["alwabrn.integration.account"].with_user(self.employee).search([]).read(["name"])

    def test_09_posted_is_immutable_and_reversal(self):
        self.tx("opening", self.ea, 40)
        tx = self.tx("issue_site", self.ea, 10, site=self.site)
        with self.assertRaises(UserError):
            tx.unlink()
        with self.assertRaises(UserError):
            tx.write({"date": fields.Datetime.now()})
        tx.with_user(self.manager).action_reverse()
        self.assertEqual(tx.state, "reversed")
        self.assertEqual(self.qty(self.ea), 40)
        self.assertEqual(self.qty(self.ea, self.site.location_id), 0)
        with self.assertRaises(UserError):
            tx.with_user(self.manager).action_reverse()

    def test_10_idempotent_posting(self):
        tx = self.tx("receipt", self.ea, 7)
        tx.with_user(self.manager).action_approve()  # retry / double click
        tx.sudo()._post()
        self.assertEqual(self.qty(self.ea), 7)
        self.assertEqual(len(tx.picking_id), 1)

    def test_11_backdated_movement_and_historical_cutoff(self):
        from ..models.common import riyadh_day_end_utc, riyadh_today
        today = riyadh_today()
        self.tx("opening", self.ea, 100, date=fields.Datetime.now() - timedelta(days=5))
        self.tx("receipt", self.ea, 30, date=fields.Datetime.now() - timedelta(days=3))
        self.tx("receipt", self.ea, 9)  # today
        bal = lambda d: self.svc.balance_at(self.wh, riyadh_day_end_utc(today - timedelta(days=d))).get(self.ea.id, 0)  # noqa
        self.assertEqual(bal(6), 0)
        self.assertEqual(bal(4), 100)
        self.assertEqual(bal(2), 130)
        self.assertEqual(bal(0), 139)
        # reconciliation for a past date ignores later movements
        snap = self.env["alwabrn.daily.snapshot"].create({
            "balance_date": today - timedelta(days=2),
            "line_ids": [(0, 0, {"product_id": self.ea.id, "quantity": 130})]})
        snap.with_user(self.manager).action_approve()
        rec = self.env["alwabrn.reconciliation"].create({"date": today - timedelta(days=2), "book_snapshot_id": snap.id})
        rec.action_compute()
        self.assertEqual(rec.line_ids.status, "match")

    def test_12_internal_transfer_keeps_company_total(self):
        self.tx("opening", self.ea, 60)
        before = self.company_qty(self.ea)
        self.tx("issue_site", self.ea, 25, site=self.site)
        self.assertEqual(self.company_qty(self.ea), before)
        self.tx("damaged", self.ea, 2, site=self.site)
        self.assertEqual(self.company_qty(self.ea), before, "damaged stock is still company custody")
        self.tx("scrap", self.ea, 1, site=self.site)
        self.assertEqual(self.company_qty(self.ea), before - 1)

    def test_13_opening_only_once_and_classification(self):
        self.tx("opening", self.ea, 10)
        tx = self.tx("opening", self.ea, 10, post=False)
        tx.action_submit()
        with self.assertRaises(UserError):
            tx.with_user(self.manager).action_approve()
        # an unapproved receipt of 4 explains a +4 book difference
        self.tx("receipt", self.ea, 4, post=False).action_submit()
        snap = self._snapshot(self.ea, 14)
        rec = self.env["alwabrn.reconciliation"].create({"book_snapshot_id": snap.id})
        rec.action_compute()
        self.assertEqual(rec.line_ids.classification, "unconfirmed")

    def test_14_dashboard_and_reports(self):
        p = self.base_scenario()
        data = self.env["alwabrn.dashboard"].with_user(self.auditor).get_data({})
        k = {x["key"]: x["value"] for x in data["kpis"]}
        self.assertEqual(k["materials"], 624)
        self.assertEqual(k["in_today"], 2)   # receipt + return
        self.assertEqual(k["out_today"], 1)  # issue to site
        self.assertEqual(k["custody_items"], 1)
        self.assertAlmostEqual(k["wh_value"], 115 * p.alw_reference_price, places=2)
        Engine = self.env["alwabrn.report.engine"]
        rep = Engine.get_report("current_stock", {})
        row = [r for r in rep["rows"] if r["sec"] == p.alw_sec_code][0]
        self.assertEqual(row["qty"], 115)
        rep = Engine.get_report("custody", {})
        self.assertEqual([r["out"] for r in rep["rows"] if r["sec"] == p.alw_sec_code], [15])
        rep = Engine.get_report("monthly_summary", {})
        row = [r for r in rep["rows"] if r["sec"] == p.alw_sec_code][0]
        self.assertEqual(row["open"] + row["in"] + row["ret"] - row["iss"] + row["adj"], row["close"])
        from ..models.report_engine import REPORTS
        for code, _label in REPORTS:
            content = Engine.render_xlsx(Engine.get_report(code, {}))
            self.assertTrue(content[:2] == b"PK", code)

    def test_15_xlsx_formula_injection(self):
        import io
        import openpyxl
        self.ea.product_tmpl_id.name = "=HYPERLINK(\"http://evil\",\"x\")"
        self.tx("opening", self.ea, 1)
        Engine = self.env["alwabrn.report.engine"]
        wb = openpyxl.load_workbook(io.BytesIO(Engine.render_xlsx(Engine.get_report("current_stock", {}))))
        ws = wb["التفاصيل"]
        cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and "HYPERLINK" in c.value]
        self.assertTrue(cells)
        for c in cells:
            self.assertEqual(c.data_type, "s")
            self.assertTrue(c.value.startswith("'"))

    def test_16_scheduled_report_no_duplicate(self):
        import pytz
        from datetime import datetime
        partner = self.env["res.partner"].create({"name": "مدير", "email": "boss@example.com"})
        sch = self.env["alwabrn.report.schedule"].create({
            "name": "يومي", "report_code": "current_stock", "frequency": "daily", "hour": 6,
            "recipient_ids": [(6, 0, partner.ids)], "active": True})
        now = pytz.timezone("Asia/Riyadh").localize(datetime(2026, 11, 16, 7, 0)).astimezone(pytz.utc)
        Model = self.env["alwabrn.report.schedule"].with_context(alw_no_send=True)
        Model.cron_run(now)
        Model.cron_run(now)                       # retry
        sch.last_run_key = False                  # simulate crash before the key was saved
        Model.cron_run(now)
        hist = self.env["alwabrn.report.history"].search([("schedule_id", "=", sch.id)])
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist.recipients, "boss@example.com")
        inactive = sch.copy({"active": False})
        Model.cron_run(now)
        self.assertFalse(inactive.history_ids, "inactive schedule must never send")

    def test_17_assistant_uses_real_data(self):
        p = self.base_scenario()
        A = self.env["alwabrn.assistant"].with_user(self.auditor)
        res = A.ask("كم رصيد المادة %s؟" % p.alw_sec_code)
        self.assertEqual(res["rows"][0][1], 115)
        self.assertEqual(res["rows"][1][1], 15)
        self.assertIn("غير موجود", A.ask("كم رصيد المادة 999999999؟")["text"])
        res = A.ask("ما المواد الموجودة بعهدة الموقع 105؟")
        self.assertEqual(res["rows"][0][6], 15)
        self.assertEqual(len(A.ask("ما المواد التي دخلت اليوم؟")["rows"]), 2)
        self.assertEqual(self.qty(p), 115)

    def test_18_audit_trail_immutable(self):
        self.tx("receipt", self.ea, 1)
        log = self.env["alwabrn.approval"].search([], limit=1)
        with self.assertRaises(UserError):
            log.write({"note": "x"})
        with self.assertRaises(UserError):
            log.unlink()
