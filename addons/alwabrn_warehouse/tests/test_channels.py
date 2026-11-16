import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, tagged

from .common import AlwCase

OCR = "odoo.addons.alwabrn_warehouse.models.source_document.AlwabrnOcrExtraction._ocr_tesseract"


@tagged("post_install", "-at_install", "alwabrn")
class TestChannels(AlwCase):

    def _paste(self, text, content_type="unknown", channel="paste"):
        wiz = self.env["alwabrn.paste.wizard"].with_user(self.employee).create(
            {"text": text, "content_type": content_type, "channel": channel})
        return self.env["alwabrn.import.batch"].browse(wiz.action_process()["res_id"])

    def test_01_paste_requires_classification(self):
        sec = self.ea.alw_sec_code
        batch = self._paste("%s    100\n908111002    250" % sec)
        self.assertEqual(batch.content_type, "unknown")
        self.assertEqual(batch.state, "needs_review")
        self.assertEqual(batch.line_ids.mapped("quantity"), [100, 250])
        with self.assertRaises(UserError):  # never assume a receipt
            batch.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 0)

    def test_02_excel_snapshot_does_not_change_stock(self):
        p = self.base_scenario()
        wiz = self.env["alwabrn.upload.wizard"].with_user(self.employee).create({
            "channel": "excel", "content_type": "book_balance",
            "file": base64.b64encode(self.xlsx([(int(p.alw_sec_code), 110, "EA")])), "filename": "balance.xlsx"})
        batch = self.env["alwabrn.import.batch"].browse(wiz.action_process()["res_id"])
        self.assertEqual(batch.state, "validated")
        with self.assertRaises(AccessError):
            batch.with_user(self.employee).action_approve()
        batch.with_user(self.manager).action_approve()
        self.assertEqual(batch.state, "approved")
        self.assertFalse(batch.transaction_ids)
        self.assertEqual(self.qty(p), 115)
        rec_id = batch.snapshot_ids.action_reconcile()["res_id"]
        line = self.env["alwabrn.reconciliation"].browse(rec_id).line_ids
        self.assertEqual((line.odoo_qty, line.book_qty, line.difference), (115, 110, -5))
        self.assertEqual(self.qty(p), 115)

    def test_03_excel_validation(self):
        sec = self.ea.alw_sec_code
        content = self.xlsx([(int(sec), 5, "EA"), (int(sec), 5, "EA"), (123456789, 3, "EA"), (None, 4, "EA"),
                             (int(sec), "abc", "EA"), (int(sec), 2, "XYZ"), (int(sec), 2, "M")])
        batch = self.env["alwabrn.import.batch"].create({"channel": "excel", "content_type": "receipt"})
        batch.load_table(content, "x.xlsx")
        batch.action_validate()
        codes = [set((l.issue_codes or "").split(",")) - {""} for l in batch.line_ids]
        self.assertEqual(codes[0], set())
        self.assertIn("dup_in_batch", codes[1])
        self.assertIn("unknown_material", codes[2])
        self.assertIn("no_sec_code", codes[3])
        self.assertIn("bad_quantity", codes[4])
        self.assertIn("bad_unit", codes[5])
        self.assertIn("unit_incompatible", codes[6])
        self.assertFalse(batch.line_ids[2].product_id, "unknown codes are never matched to a guessed material")
        with self.assertRaises(UserError):
            batch.with_user(self.manager).action_approve()
        action = batch.action_download_validation_report()
        self.assertIn("/web/content/", action["url"])

    def test_04_whatsapp_text_draft_then_post(self):
        self.tx("opening", self.p1, 50)
        batch = self._paste("تم صرف 10 من المادة 908111001 إلى موقع 105", channel="whatsapp")
        self.assertEqual(batch.content_type, "issue_site")
        self.assertEqual(batch.line_ids.site_id, self.site)
        self.assertEqual(batch.state, "validated")
        self.assertEqual(self.qty(self.p1), 50, "nothing is posted before approval")
        batch.with_user(self.manager).action_approve()
        self.assertEqual(batch.state, "posted")
        tx = batch.transaction_ids
        self.assertEqual((tx.tx_type, tx.state, tx.site_id), ("issue_site", "posted", self.site))
        self.assertTrue(tx.picking_id.state == "done")
        self.assertEqual(self.qty(self.p1), 40)
        self.assertEqual(self.qty(self.p1, self.site.location_id), 10)
        batch.with_user(self.manager).action_approve()  # idempotent
        self.assertEqual(self.qty(self.p1), 40)

    def _photo_batch(self, lines, seed=0):
        doc = self.env["alwabrn.source.document"].with_user(self.employee).create(
            {"name": "register.png", "datas": self.png("r", seed), "channel": "camera", "register_type": "incoming"})
        batch = self.env["alwabrn.import.batch"].with_user(self.employee).create(
            {"channel": "camera", "content_type": "receipt"})
        with patch(OCR, return_value=lines):
            batch.load_document(doc)
        batch.action_validate()
        return batch

    def test_05_duplicate_photograph(self):
        sec = self.ea.alw_sec_code
        lines = [{"text": "%s 25 EA" % sec, "conf": 95.0}]
        b1 = self._photo_batch(lines)
        b1.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 25)
        b2 = self._photo_batch(lines)  # same photograph again
        self.assertEqual(b2.duplicate_of_id, b1)
        self.assertEqual(b2.state, "needs_review")
        with self.assertRaises(UserError):
            b2.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 25)
        # a genuine second movement can still be posted after explicit manager confirmation
        b2.with_user(self.manager).allow_duplicate = True
        b2.line_ids.write({"reviewed": True})
        b2.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 50)
        self.assertTrue(self.env["alwabrn.approval"].search([("action", "=", "duplicate_override")]))

    def test_06_ocr_uncertainty(self):
        sec = self.ea.alw_sec_code
        batch = self._photo_batch([{"text": "90811?0?1 12", "conf": 31.0}, {"text": "%s 7" % sec, "conf": 40.0}])
        bad, low = batch.line_ids
        self.assertFalse(bad.product_id)
        self.assertFalse(bad.sec_code, "no code is invented for unreadable text")
        self.assertEqual(bad.status, "error")
        self.assertIn("low_confidence", low.issue_codes)
        with self.assertRaises(UserError):
            batch.with_user(self.manager).action_approve()
        bad.excluded = True
        with self.assertRaises(UserError):  # low-confidence line must be reviewed by a human
            batch.with_user(self.manager).action_approve()
        low.reviewed = True
        batch.with_user(self.manager).action_approve()
        self.assertEqual(self.qty(self.ea), 7)

    def test_07_failed_ocr_is_reported_not_raised(self):
        doc = self.env["alwabrn.source.document"].create({"name": "r.png", "datas": self.png("z", 3), "channel": "image"})
        batch = self.env["alwabrn.import.batch"].create({"channel": "image", "content_type": "receipt"})
        with patch(OCR, side_effect=RuntimeError("engine crashed")):
            batch.load_document(doc)
        self.assertEqual(doc.extraction_ids.state, "failed")
        self.assertIn("engine crashed", batch.error_message)
        self.assertFalse(batch.line_ids)

    def test_08_file_validation(self):
        with self.assertRaises(Exception):
            self.env["alwabrn.source.document"].create(
                {"name": "x.exe", "datas": base64.b64encode(b"MZ\x90\x00" + b"\x00" * 200), "channel": "image"})

    def test_09_unit_conversion_in_import(self):
        self.tx("opening", self.p1, 2)
        batch = self._paste("908111001 500 M", content_type="book_balance")
        line = batch.line_ids
        self.assertEqual((line.quantity, line.uom_id), (500, self.m))
        self.assertAlmostEqual(line.qty_product_uom, 0.5, places=6)
        batch.with_user(self.manager).action_approve()
        sl = batch.snapshot_ids.line_ids
        self.assertEqual((sl.quantity, sl.uom_id), (500, self.m))  # original entry preserved
        self.assertAlmostEqual(sl.qty_product_uom, 0.5, places=6)

    # ------------------------------------------------------------- e-mail
    def _mail(self, message_id, sender, attachments=None, body=""):
        return self.env["alwabrn.import.batch"].message_new({
            "message_id": message_id, "email_from": sender, "subject": "رصيد اليوم", "body": body,
            "attachments": attachments or []})

    def test_10_email_attachment(self):
        self.env.ref("alwabrn_warehouse.integration_email").allowed_senders = "store@alwabrn.example"
        p = self.base_scenario()
        att = [("balance.xlsx", self.xlsx([(int(p.alw_sec_code), 110, "EA")]))]
        b1 = self.env["alwabrn.import.batch"].browse(self._mail("<m1@x>", "Store <store@alwabrn.example>", att))
        self.assertEqual(b1.channel, "email")
        self.assertIn(b1.state, ("needs_review", "validated", "draft"))
        self.assertEqual(len(b1.line_ids), 1)
        self.assertFalse(b1.transaction_ids)
        self.assertEqual(self.qty(p), 115, "e-mail import is never posted automatically")
        b_again = self._mail("<m1@x>", "store@alwabrn.example", att)  # fetch retry
        self.assertEqual(b_again, b1.id)
        self.assertEqual(self.env["alwabrn.import.batch"].search_count([("external_message_id", "=", "<m1@x>")]), 1)
        # unauthorized sender: content is not processed
        b3 = self.env["alwabrn.import.batch"].browse(self._mail("<m2@x>", "evil@other.example", att))
        self.assertEqual(b3.state, "rejected")
        self.assertFalse(b3.line_ids)

    # ----------------------------------------------------------- whatsapp
    def _wa_account(self):
        acc = self.env.ref("alwabrn_warehouse.integration_whatsapp")
        acc.write({"wa_phone_number_id": "123", "allowed_senders": "966500000001"})
        for k, v in (("access_token", "tok"), ("app_secret", "s3cr3t"), ("verify_token", "verify-me")):
            acc.set_secret(k, v)
        return acc

    def _wa_payload(self, msg_id, sender, text):
        return {"entry": [{"changes": [{"value": {"messages": [
            {"id": msg_id, "from": sender, "timestamp": "1794800000", "type": "text", "text": {"body": text}}]}}]}]}

    def test_11_whatsapp_business_adapter(self):
        acc = self._wa_account()
        self.assertEqual(acc.state, "auth_required", "never 'connected' before a real verified exchange")
        self.assertNotIn("s3cr3t", acc.sudo().secret_blob)  # encrypted at rest
        self.tx("opening", self.p1, 50)
        payload = self._wa_payload("wamid.1", "966500000001", "تم صرف 10 من المادة 908111001 إلى موقع 105")
        batches = acc.wa_process_payload(payload)
        self.assertEqual(len(batches), 1)
        self.assertEqual((batches.content_type, batches.channel), ("issue_site", "whatsapp"))
        self.assertEqual(self.qty(self.p1), 50, "never auto-approved")
        self.assertFalse(acc.wa_process_payload(payload), "webhook retry must not create a second batch")
        self.assertFalse(acc.wa_process_payload(self._wa_payload("wamid.2", "966599999999", "908111001 5")))
        msg = self.env["alwabrn.integration.message"].search([("external_id", "=", "wamid.2")])
        self.assertEqual(msg.state, "unauthorized")
        # notifications are not sent unless connected + enabled
        self.assertFalse(acc.wa_send_text("966500000001", "x"))

    def test_12_signature(self):
        acc = self._wa_account()
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        self.assertTrue(acc.wa_check_signature(body, good))
        self.assertFalse(acc.wa_check_signature(body, "sha256=" + "0" * 64))
        self.assertFalse(acc.wa_check_signature(body, None))


@tagged("post_install", "-at_install", "alwabrn")
class TestWebhookHttp(HttpCase):

    def test_webhook_security(self):
        acc = self.env.ref("alwabrn_warehouse.integration_whatsapp")
        acc.write({"wa_phone_number_id": "123", "allowed_senders": "966500000001"})
        for k, v in (("access_token", "tok"), ("app_secret", "s3cr3t"), ("verify_token", "verify-me")):
            acc.set_secret(k, v)
        url = "/alwabrn/whatsapp/webhook"
        r = self.url_open(url + "?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=42")
        self.assertEqual(r.status_code, 403)
        r = self.url_open(url + "?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=42")
        self.assertEqual((r.status_code, r.text), (200, "42"))
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid.http1", "from": "966500000001", "timestamp": "1794800000", "type": "text",
             "text": {"body": "908111001 5"}}]}}]}]}).encode()
        r = self.url_open(url, data=body, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403, "unsigned request must be rejected")
        r = self.url_open(url, data=body, headers={"Content-Type": "application/json",
                                                    "X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(r.status_code, 403)
        sig = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        r = self.url_open(url, data=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.env["alwabrn.import.batch"].search_count([("external_message_id", "=", "wamid.http1")]), 1)


@tagged("post_install", "-at_install", "alwabrn")
class TestUnits(AlwCase):

    def test_all_catalog_units_resolve(self):
        for code in ("EA", "KM", "M", "KIT", "R", "DRUM"):
            uom = self.svc.uom_from_text(code)
            self.assertTrue(uom and uom.active, code)
        self.assertEqual(self.svc.uom_from_text("كم"), self.km)
        self.assertEqual(self.svc.uom_from_text("متر"), self.m)
        self.assertFalse(self.svc.uom_from_text("XYZ"))

    def test_paste_with_km_unit(self):
        batch = self.env["alwabrn.import.batch"].create({"channel": "paste", "content_type": "book_balance"})
        batch.load_text("908111001 25 KM")
        batch.action_validate()
        self.assertEqual((batch.line_ids.status, batch.line_ids.uom_id), ("ok", self.km))
