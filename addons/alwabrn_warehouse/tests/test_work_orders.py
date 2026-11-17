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
        line = wo.material_ids.filtered(lambda l: l.product_id == self.ea)      # created automatically
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
        line = wo.material_ids.filtered(lambda l: l.product_id == self.ea)
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

    def test_07_full_work_order_ledger_and_report(self):
        WO = self.env["alwabrn.work.order"]
        wo = WO.create({"name": "254099001"})
        p = self.ea
        self.tx("receipt", p, 40, work_order_id=wo.id)                      # received from SEC for this WO
        site = wo._ensure_site()
        self.tx("issue_site", p, 30, site=site, work_order_id=wo.id)        # delivered to site
        self.tx("consume", p, 20, site=site, work_order_id=wo.id)           # installed
        self.tx("return_in", p, 4, site=site, work_order_id=wo.id)          # back to warehouse
        self.tx("return_out", p, 2, work_order_id=wo.id)                    # returned to SEC
        line = wo.material_ids.filtered(lambda l: l.product_id == p)
        self.assertEqual(len(line), 1, "material line is created automatically from the movements")
        self.assertEqual((line.wh_received, line.wh_issued, line.erected, line.wh_returned, line.wh_returned_sec, line.wh_net),
                         (40, 30, 20, 4, 2, 6))
        self.assertEqual(self.qty(p), 40 - 30 + 4 - 2)
        rep = self.env["alwabrn.report.engine"].get_report("work_order", {"work_order_id": wo.id})
        row = [r for r in rep["rows"] if r["sec"] == p.alw_sec_code][0]
        self.assertEqual((row["rec"], row["iss"], row["erc"], row["ret"], row["ret_sec"], row["net"]), (40, 30, 20, 4, 2, 6))
        self.assertIn(("أمر العمل", wo.name), rep["summary"])
        self.assertIn("أمر العمل: %s" % wo.name, rep["filters"])
        content = self.env["alwabrn.report.engine"].render_xlsx(rep)
        self.assertEqual(content[:2], b"PK")
        mv = self.env["alwabrn.report.engine"].get_report("movements", {"work_order_id": wo.id, "date_from": "2015-01-01"})
        self.assertEqual(len(mv["rows"]), 5)
        self.assertTrue(all(r["wo"] == wo.name for r in mv["rows"]))
        self.assertEqual(wo.action_report()["context"]["default_work_order_id"], wo.id)

    def _wiz(self, rec, method, content, filename):
        import base64
        act = getattr(rec, "action_import_" + method)()
        return self.env["alwabrn.line.import.wizard"].with_context(act["context"]).create(
            {"file": base64.b64encode(content), "filename": filename})

    def test_08_import_lines_excel_into_draft_issue(self):
        self.tx("opening", self.ea, 100)
        tx = self.Tx.create({"tx_type": "issue_site", "site_id": self.site.id})
        content = self.xlsx([(int(self.ea.alw_sec_code), 12, "EA"), (int(self.p1.alw_sec_code), 500, "M"), (123456789, 3, "EA")])
        wiz = self._wiz(tx, "excel", content, "issue.xlsx")
        wiz.action_extract()
        self.assertEqual(wiz.state, "preview")
        self.assertEqual(len(wiz.line_ids), 3)
        unknown = wiz.line_ids.filtered(lambda l: l.sec_code == "123456789")
        self.assertFalse(unknown.include or unknown.product_id, "unknown code is never matched or added")
        wiz.action_apply()
        self.assertEqual(len(tx.line_ids), 2)
        cable = tx.line_ids.filtered(lambda l: l.product_id == self.p1)
        self.assertEqual((cable.quantity, cable.uom_id), (500, self.m))
        self.assertAlmostEqual(cable.qty_product_uom, 0.5, places=6)
        self.assertEqual(tx.state, "draft", "import only fills the draft — nothing is posted")
        self.assertEqual(self.qty(self.ea), 100)
        self.assertTrue(tx.source_document_ids)

    def test_09_import_lines_camera_and_snapshot(self):
        from unittest.mock import patch
        OCR = "odoo.addons.alwabrn_warehouse.models.source_document.AlwabrnOcrExtraction._ocr_tesseract"
        tx = self.Tx.create({"tx_type": "receipt"})
        lines = [{"text": "%s 7" % self.ea.alw_sec_code, "conf": 95.0}, {"text": "9081?1?01 3", "conf": 30.0}]
        wiz = self._wiz(tx, "camera", __import__("base64").b64decode(self.png("x", 9)), "camera.jpg")
        with patch(OCR, return_value=lines):
            wiz.action_extract()
        self.assertEqual(len(wiz.line_ids.filtered("include")), 1, "unreadable/uncertain line is not pre-selected")
        self.assertTrue(wiz.preview_image)
        wiz.action_apply()
        self.assertEqual(tx.line_ids.quantity, 7)
        # balance statement: same material twice is merged
        snap = self.env["alwabrn.daily.snapshot"].create({"line_ids": [(0, 0, {"product_id": self.ea.id, "quantity": 5})]})
        wiz = self._wiz(snap, "excel", self.xlsx([(int(self.ea.alw_sec_code), 4, "EA")]), "bal.xlsx")
        wiz.action_extract()
        wiz.action_apply()
        self.assertEqual((len(snap.line_ids), snap.line_ids.quantity), (1, 9))
        # posted documents cannot receive lines
        posted = self.tx("receipt", self.ea, 1)
        with self.assertRaises(UserError):
            self._wiz(posted, "excel", self.xlsx([(int(self.ea.alw_sec_code), 1, "EA")]), "x.xlsx").action_extract()

    def test_10_explorer_dashboard_and_material_report(self):
        X = self.env["alwabrn.explorer"].with_user(self.auditor)
        wo = self.env["alwabrn.work.order"].create({"name": "254099777", "stage": "delivered"})
        site = wo._ensure_site()
        p = self.ea
        self.tx("receipt", p, 50, work_order_id=wo.id)
        self.tx("issue_site", p, 30, site=site, work_order_id=wo.id)
        self.tx("return_in", p, 10, site=site, work_order_id=wo.id)
        self.tx("issue", p, 5)                                               # no work order
        data = X.get_data({"sec_code": p.alw_sec_code})
        self.assertTrue(data["single"])
        row = data["materials"][0]
        self.assertEqual((row["warehouse"], row["custody"], row["rec"], row["iss"], row["ret"]), (25, 20, 50, 35, 10))
        k = {x["key"]: x["display"] for x in data["kpis"]}
        self.assertTrue(k["wh"].startswith("25"))
        wrow = [w for w in data["work_orders"] if w["name"] == wo.name][0]
        self.assertEqual((wrow["q"]["iss"], wrow["q"]["ret"]), (30, 10))
        # stage filter + work order filter
        self.assertEqual(X.get_data({"stage": "drawing", "sec_code": p.alw_sec_code})["wo_total"], 0)
        data = X.get_data({"wo_text": wo.name})
        self.assertEqual([r["iss"] for r in data["materials"] if r["id"] == p.id], [30])
        self.assertIn("غير موجود", X.get_data({"sec_code": "999999999"})["filters"]["not_found"])
        # material ledger report: per work order + rows without work order
        rep = self.env["alwabrn.report.engine"].get_report("material_ledger", {"product_id": p.id, "date_from": "2015-01-01"})
        by_wo = {r["wo"]: r for r in rep["rows"]}
        self.assertEqual((by_wo[wo.name]["rec"], by_wo[wo.name]["iss"], by_wo[wo.name]["ret"], by_wo[wo.name]["net"]), (50, 30, 10, 20))
        self.assertEqual(by_wo["بدون أمر عمل"]["iss"], 5)
        # stage filter on the work order report
        self.assertEqual(self.env["alwabrn.report.engine"].get_report("work_order", {"stage": "extract"})["rows"], [])
        self.assertTrue(self.env["alwabrn.report.engine"].get_report("work_order", {"stage": "delivered", "product_id": p.id})["rows"])
        urls = self.env["alwabrn.explorer"].export("material_ledger", {"sec_code": p.alw_sec_code}, "xlsx")
        self.assertEqual(len(urls), 1)
        self.assertIn("/web/content/", urls[0])
