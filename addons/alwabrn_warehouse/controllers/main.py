import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class AlwabrnWebhook(http.Controller):

    def _account(self):
        return request.env["alwabrn.integration.account"].sudo().search(
            [("kind", "=", "whatsapp"), ("active", "=", True)], limit=1)

    @http.route("/alwabrn/whatsapp/webhook", type="http", auth="public", methods=["GET"], csrf=False)
    def whatsapp_verify(self, **kw):
        """Meta webhook verification handshake."""
        acc = self._account()
        token = acc._secrets().get("verify_token") if acc else None
        given = kw.get("hub.verify_token") or ""
        if token and kw.get("hub.mode") == "subscribe" and hmac.compare_digest(token, given):
            acc.wa_webhook_verified = True
            acc.log_event("whatsapp", "info", "تم التحقق من الـ Webhook من Meta.")
            return request.make_response(kw.get("hub.challenge") or "", [("Content-Type", "text/plain")])
        return request.make_response("forbidden", status=403)

    @http.route("/alwabrn/whatsapp/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def whatsapp_receive(self, **kw):
        acc = self._account()
        raw = request.httprequest.get_data()
        signature = request.httprequest.headers.get("X-Hub-Signature-256")
        if not acc or not acc.wa_check_signature(raw, signature):
            _logger.warning("WhatsApp webhook rejected: bad signature")
            if acc:
                acc.log_event("whatsapp", "warning", "طلب Webhook مرفوض: توقيع غير صحيح.")
            return request.make_response("invalid signature", status=403)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return request.make_response("bad payload", status=400)
        acc.wa_process_payload(payload)
        return request.make_response("ok", [("Content-Type", "text/plain")])
