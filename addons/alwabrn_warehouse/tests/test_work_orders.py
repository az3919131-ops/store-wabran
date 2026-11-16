from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from ..tools import wo_excel
from .common import AlwCase

REQ = "odoo.addons.alwabrn_warehouse.models.work_order.requests"


def _resp(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status < 400 else __import__("requests").HTTPError("HTTP %s" % status)
    return r


@tagged("post_install", "-at_install", "alwabrn")
class TestWorkOrders(AlwCase):

    def sheets(self):
        hdr = ["رقم أمر العمل ", "رمز نوع أمر العمل", "نوع امر العمل", "تاريخ التسليم للمقاول ", "تكلفة العمالة ", "الموقف", "المسؤل"]
        return [
            ("تم التسليم ", [["تم تسليم المقاول ولم يتم الاستلام - نجران"], hdr,
                             [254043523.0, 801.0, "تركيب عداد", "2026/02/25", 374.6, "ارجاع مواد", "ضياء"],
                             [254043523.0, 804.0, "تركيب محطة", 46265.0, 14753.0, "تم تسليم اللجنة", "م محمود"],
                             ["الإجمالي", "", "", "", 15127.6, "", ""]]),
            ("دخلت مستخلص ولم تصرف", [hdr, [224004913.0, 401.0, "توصيل عداد", "2022/03/23", 571.2, "", ""]]),
        ]

    def test_01_excel_import_idempotent(self):
        items, problems = wo_excel.parse(self.sheets())
        self.assertEqual(len(items), 3)
        self.assertEqual(len(problems), 1)            # total row reported, not imported
        self.assertEqual(str(items[1]["delivery_date"]), "2026-08-31")   # Excel serial date
        WO = self.env["alwabrn.work.order"]
        s1 = WO.import_items(items)
        self.assertEqual((s1["work_orders_new"], s1["rows_new"]), (2, 3))
        wo = WO.search([("name", "=", "254043523")])
        self.assertEqual(wo.stage, "delivered")
        self.assertEqual(len(wo.item_ids), 2)
        self.assertAlmostEqual(wo.labor_cost, 15127.6, places=2)
        self.assertIn("801", wo.type_summary)
        s2 = WO.import_items(items)                   # re-import: update only
        self.assertEqual((s2["work_orders_new"], s2["rows_new"], s2["rows_updated"]), (0, 0, 3))
        self.assertEqual(WO.search_count([("name", "in", ["254043523", "224004913"])]), 2)
        with self.assertRaises(UserError):
            WO.create({"name": "ABC-12"})

    def test_02_issue_and_return_by_work_order(self):
        self.tx("opening", self.ea, 50)
        wo = self.env["alwabrn.work.order"].create({"name": "264051257"})
        ctx = wo.action_issue()["context"]
        self.assertEqual(ctx["default_tx_type"], "issue_site")
        site = wo.site_id
        self.assertTrue(site and site.location_id)
        tx = self.tx("issue_site", self.ea, 20, site=site, work_order_id=wo.id)
        self.tx("return_in", self.ea, 5, site=site, work_order_id=wo.id)
        self.assertEqual(self.qty(self.ea), 35)
        self.assertEqual(self.qty(self.ea, site.location_id), 15)
        self.assertEqual(tx.work_order_id, wo)
        # material line (e.g. created from minutes) shows warehouse issue/return for the work order
        line = self.env["alwabrn.work.order.material"].create({
            "work_order_id": wo.id, "sec_code": self.ea.alw_sec_code, "product_id": self.ea.id})
        self.assertEqual((line.wh_issued, line.wh_returned, line.wh_net), (20, 5, 15))
        # cannot return more than the work order custody
        bad = self.tx("return_in", self.ea, 16, site=site, work_order_id=wo.id, post=False)
        bad.action_submit()
        with self.assertRaises(UserError):
            bad.with_user(self.manager).action_approve()

    def test_03_return_of_material_issued_before_the_system(self):
        wo = self.env["alwabrn.work.order"].create({"name": "214015327"})
        tx = self.tx("return_legacy", self.ea, 7, work_order_id=wo.id)
        self.assertEqual(tx.state, "posted")
        self.assertEqual(self.qty(self.ea), 7)
        line = self.env["alwabrn.work.order.material"].create({
            "work_order_id": wo.id, "sec_code": self.ea.alw_sec_code, "product_id": self.ea.id})
        self.assertEqual(line.wh_returned, 7)
        no_wo = self.tx("return_legacy", self.ea, 1, post=False)
        with self.assertRaises(UserError):
            no_wo.action_submit()

    def _conf(self):
        acc = self.env.ref("alwabrn_warehouse.integration_minutes")
        acc.minutes_url = "https://minutes.example"
        acc.set_secret("api_key", "k" * 30)
        return acc

    def test_04_minutes_sync(self):
        acc = self._conf()
        WO = self.env["alwabrn.work.order"]
        old = WO.create({"name": "254043523", "stage": "delivered", "source": "excel"})
        sec = self.p1.alw_sec_code
        listing = {"items": {"m1": "e1", "m2": "e1", "bad": "e1"}}
        docs = {"docs": {
            "m1": {"id": "m1", "wo": "254043523", "requestNo": "254043523-801", "status": "معتمد", "contractor": "الوبران",
                   "location": "الفيصلية", "materials": [{"sec": sec, "name": "CABLE", "unit": "KM", "issued": 0.5, "returned": 0.1, "erected": 0.4}]},
            "m2": {"id": "m2", "wo": "999888777", "requestNo": "999888777", "contractor": "مقاول 2",
                   "materials": [{"sec": sec, "issued": 1.0}, {"sec": "123456789", "issued": 3}]},
            "bad": {"id": "bad", "wo": "abc"},
        }}
        with patch(REQ + ".get", return_value=_resp(listing)) as g, patch(REQ + ".post", return_value=_resp(docs)):
            stats = WO.sync_minutes()
        self.assertEqual(g.call_args.kwargs["headers"]["x-integration-key"], "k" * 30)
        self.assertEqual((stats["remote"], stats["changed"], stats["new_work_orders"]), (3, 3, 1))
        self.assertEqual(old.minutes_count, 1)
        self.assertEqual(old.contractor, "الوبران")
        line = old.material_ids.filtered(lambda l: l.sec_code == sec)
        self.assertEqual((line.sec_issued, line.sec_returned, line.sec_erected), (0.5, 0.1, 0.4))
        self.assertEqual(line.product_id, self.p1)
        new = WO.search([("name", "=", "999888777")])
        self.assertEqual((new.source, new.stage), ("minutes", "minutes"))
        self.assertEqual(len(new.material_ids), 2)
        self.assertFalse(new.material_ids.filtered(lambda l: l.sec_code == "123456789").product_id, "unknown code is not guessed")
        self.assertEqual(acc.state, "connected")
        # second run: unchanged etags -> nothing fetched; m2 deleted on the platform -> removed and quantities cleared
        listing2 = {"items": {"m1": "e1", "bad": "e1"}}
        with patch(REQ + ".get", return_value=_resp(listing2)), patch(REQ + ".post") as p:
            stats = WO.sync_minutes()
        self.assertEqual((stats["changed"], stats["removed"]), (1, 1))   # only the invalid one is retried
        self.assertEqual(new.minutes_count, 0)
        self.assertEqual(new.material_ids.mapped("sec_issued"), [0.0, 0.0])
        # changed minute updates the quantities instead of adding
        docs["docs"]["m1"]["materials"][0]["issued"] = 0.7
        with patch(REQ + ".get", return_value=_resp({"items": {"m1": "e2"}})), patch(REQ + ".post", return_value=_resp(docs)):
            WO.sync_minutes()
        self.assertEqual(line.sec_issued, 0.7)

    def test_05_minutes_sync_errors_are_logged_not_raised(self):
        import requests
        self._conf()
        WO = self.env["alwabrn.work.order"]
        with patch(REQ + ".get", side_effect=requests.ConnectionError("down")):
            self.assertIn("error", WO.sync_minutes())
            with self.assertRaises(UserError):
                WO.sync_minutes(raise_errors=True)
        acc = self.env.ref("alwabrn_warehouse.integration_minutes")
        self.assertIn("down", acc.last_error)
        acc.set_secret("api_key", False)
        acc.minutes_url = False
        self.assertIn("error", WO.sync_minutes())

    def test_06_list_buttons_and_permissions(self):
        WO = self.env["alwabrn.work.order"]
        arch = self.env.ref("alwabrn_warehouse.view_wo_list").arch
        self.assertIn("action_sync_minutes", arch)
        self.assertIn('display="always"', arch)
        self.assertTrue(WO.with_user(self.auditor).search([]) is not None)
        with self.assertRaises(Exception):
            WO.with_user(self.auditor).create({"name": "123456789"})
        WO.with_user(self.employee).create({"name": "123456789"})
