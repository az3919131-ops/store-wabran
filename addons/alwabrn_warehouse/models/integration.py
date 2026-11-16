import base64
import hashlib
import hmac
import logging
import os

import requests
from cryptography.fernet import Fernet, InvalidToken

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"

KINDS = [
    ("whatsapp", "واتساب للأعمال (Meta Cloud API)"),
    ("email", "البريد الإلكتروني (Gmail / Outlook / IMAP)"),
    ("ocr", "OCR / قراءة الصور"),
    ("ai", "المساعد الذكي / AI Vision"),
]
STATES = [
    ("connected", "متصل"), ("disconnected", "غير متصل"), ("auth_required", "يتطلب مصادقة / اختبار"),
    ("pending_config", "بانتظار الإعداد"),
]


class AlwabrnIntegrationAccount(models.Model):
    _name = "alwabrn.integration.account"
    _description = "حساب تكامل خارجي"
    _order = "kind"

    name = fields.Char("الاسم", required=True)
    kind = fields.Selection(KINDS, "النوع", required=True, index=True)
    active = fields.Boolean("مفعل", default=True)
    state = fields.Selection(STATES, "حالة الاتصال", compute="_compute_state")
    state_note = fields.Char("توضيح الحالة", compute="_compute_state")
    # secrets are stored encrypted (Fernet); never plain text, never logged
    secret_blob = fields.Char("بيانات سرية مشفرة", groups="alwabrn_warehouse.group_alw_admin", copy=False)
    has_access_token = fields.Boolean("تم حفظ رمز الوصول", compute="_compute_has_secrets")
    has_app_secret = fields.Boolean("تم حفظ App Secret", compute="_compute_has_secrets")
    has_verify_token = fields.Boolean("تم حفظ Verify Token", compute="_compute_has_secrets")
    has_api_key = fields.Boolean("تم حفظ API Key", compute="_compute_has_secrets")
    wa_phone_number_id = fields.Char("WhatsApp Phone Number ID")
    wa_webhook_verified = fields.Boolean("تم التحقق من الـ Webhook", readonly=True, copy=False)
    wa_notifications = fields.Boolean("السماح بإرسال الإشعارات عبر واتساب", default=False)
    wa_notify_numbers = fields.Text("أرقام مستلمي الإشعارات (سطر لكل رقم)")
    allowed_senders = fields.Text("المرسلون المصرح لهم (سطر لكل رقم/بريد)")
    fetchmail_server_id = fields.Many2one("fetchmail.server", "خادم البريد الوارد (OAuth)")
    mail_server_id = fields.Many2one("ir.mail_server", "خادم البريد الصادر (OAuth)")
    last_sync = fields.Datetime("آخر مزامنة ناجحة", readonly=True)
    last_error = fields.Text("آخر خطأ", readonly=True)
    last_error_on = fields.Datetime("وقت آخر خطأ", readonly=True)
    retry_count = fields.Integer("محاولات إعادة", readonly=True)
    log_ids = fields.One2many("alwabrn.integration.log", "account_id", "السجل")
    webhook_url = fields.Char("رابط الـ Webhook", compute="_compute_webhook_url")

    _kind_uniq = models.Constraint("unique(kind)", "يوجد حساب واحد لكل نوع تكامل.")

    # ------------------------------------------------------------- secrets
    @api.model
    def _fernet(self):
        seed = os.environ.get("ALWABRN_SECRET_KEY") or \
            self.env["ir.config_parameter"].sudo().get_param("database.secret") or ""
        if not seed:
            raise UserError("لا يوجد مفتاح تشفير. عرّف متغير البيئة ALWABRN_SECRET_KEY.")
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()))

    def _secrets(self):
        self.ensure_one()
        import json
        blob = self.sudo().secret_blob
        if not blob:
            return {}
        try:
            return json.loads(self._fernet().decrypt(blob.encode()).decode())
        except (InvalidToken, ValueError):
            _logger.error("Integration secrets cannot be decrypted (key changed?)")
            return {}

    def set_secret(self, key, value):
        import json
        if not self.env.user.has_group("alwabrn_warehouse.group_alw_admin") and not self.env.su:
            raise UserError("صلاحية مدير النظام مطلوبة.")
        for rec in self:
            data = rec._secrets()
            if value:
                data[key] = value
            else:
                data.pop(key, None)
            rec.sudo().secret_blob = rec._fernet().encrypt(json.dumps(data).encode()).decode()
            self.env["alwabrn.approval"].log(rec, "config", "تحديث بيانات اعتماد: %s" % key)

    @api.model
    def get_secret(self, kind, key):
        acc = self.sudo().search([("kind", "=", kind), ("active", "=", True)], limit=1)
        return acc._secrets().get(key) if acc else None

    def _compute_has_secrets(self):
        for rec in self:
            s = rec._secrets() if rec.id else {}
            rec.has_access_token = bool(s.get("access_token"))
            rec.has_app_secret = bool(s.get("app_secret"))
            rec.has_verify_token = bool(s.get("verify_token"))
            rec.has_api_key = bool(s.get("api_key"))

    def _compute_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for rec in self:
            rec.webhook_url = "%s/alwabrn/whatsapp/webhook" % base if rec.kind == "whatsapp" else False

    # --------------------------------------------------------------- state
    def _compute_state(self):
        for rec in self:
            state, note = "pending_config", "لم تُدخل بيانات الاعتماد بعد."
            s = rec._secrets() if rec.id else {}
            if not rec.active:
                state, note = "disconnected", "الحساب معطل."
            elif rec.kind == "whatsapp":
                if s.get("access_token") and s.get("app_secret") and s.get("verify_token") and rec.wa_phone_number_id:
                    if rec.wa_webhook_verified and rec.last_sync:
                        state, note = "connected", "تم التحقق من الـ Webhook واستقبال رسالة موقعة."
                    else:
                        state, note = "auth_required", "البيانات محفوظة — بانتظار تحقق Meta من الـ Webhook واستقبال أول رسالة."
                else:
                    note = "الاستيراد اليدوي لواتساب يعمل. الربط الآلي بانتظار بيانات WhatsApp Business."
            elif rec.kind == "email":
                srv = rec.fetchmail_server_id
                if srv and srv.state == "done":
                    state, note = "connected", "خادم البريد الوارد مؤكد."
                elif srv:
                    state, note = "auth_required", "أكمل مصادقة OAuth واختبار الخادم."
                else:
                    note = "اختر خادم بريد وارد (Gmail/Outlook عبر OAuth). لصق/رفع محتوى البريد يدوياً يعمل."
            elif rec.kind == "ocr":
                code, st = self.env["alwabrn.ocr.extraction"].provider_status()
                state = st
                note = "المزود: %s" % code
            elif rec.kind == "ai":
                if s.get("api_key"):
                    state = "connected" if rec.last_sync else "auth_required"
                    note = "المفتاح محفوظ" + ("" if rec.last_sync else " — لم يُختبر بعد.")
                else:
                    note = "المساعد يعمل بالاستعلامات المباشرة دون مزود خارجي. مفتاح AI اختياري لقراءة الصور."
            rec.state, rec.state_note = state, note

    @api.model
    def log_event(self, kind, level, message, ok=False):
        acc = self.sudo().search([("kind", "=", kind)], limit=1)
        if not acc:
            return
        self.env["alwabrn.integration.log"].sudo().create({"account_id": acc.id, "level": level, "message": message})
        if ok:
            acc.write({"last_sync": fields.Datetime.now(), "retry_count": 0})
        elif level == "error":
            acc.write({"last_error": message, "last_error_on": fields.Datetime.now(),
                       "retry_count": acc.retry_count + 1})

    def _allowed(self):
        self.ensure_one()
        return {x.strip().lower() for x in (self.allowed_senders or "").splitlines() if x.strip()}

    @api.model
    def is_email_sender_allowed(self, sender):
        acc = self.sudo().search([("kind", "=", "email"), ("active", "=", True)], limit=1)
        addr = email_normalize(sender) or (sender or "").lower()
        return bool(acc) and addr in acc._allowed()

    # ------------------------------------------------------------ whatsapp
    def wa_check_signature(self, raw_body, header):
        self.ensure_one()
        secret = self._secrets().get("app_secret")
        if not secret or not header or not header.startswith("sha256="):
            return False
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, header[7:])

    def wa_download_media(self, media_id):
        self.ensure_one()
        token = self._secrets().get("access_token")
        meta = requests.get("%s/%s" % (GRAPH, media_id), headers={"Authorization": "Bearer %s" % token}, timeout=30)
        meta.raise_for_status()
        info = meta.json()
        blob = requests.get(info["url"], headers={"Authorization": "Bearer %s" % token}, timeout=60)
        blob.raise_for_status()
        return blob.content, info.get("mime_type")

    def wa_process_payload(self, payload):
        """Process a verified webhook payload. Each message -> draft batch (never auto-approved)."""
        self.ensure_one()
        Message = self.env["alwabrn.integration.message"].sudo()
        Batch = self.env["alwabrn.import.batch"].sudo()
        allowed = {a.lstrip("+") for a in self._allowed()}
        created = Batch
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in (change.get("value") or {}).get("messages", []):
                    ext_id, sender = msg.get("id"), (msg.get("from") or "").lstrip("+")
                    if Message.search_count([("external_id", "=", ext_id), ("channel", "=", "whatsapp")]):
                        self.log_event("whatsapp", "info", "رسالة مكررة (إعادة محاولة) تم تجاهلها: %s" % ext_id)
                        continue
                    ts = fields.Datetime.to_string(fields.Datetime.now())
                    if msg.get("timestamp"):
                        from datetime import datetime, timezone
                        ts = datetime.fromtimestamp(int(msg["timestamp"]), timezone.utc).replace(tzinfo=None)
                    rec = Message.create({"channel": "whatsapp", "external_id": ext_id, "sender": sender,
                                          "message_date": ts, "body": (msg.get("text") or {}).get("body"),
                                          "state": "received"})
                    if sender not in allowed:
                        rec.state = "unauthorized"
                        self.log_event("whatsapp", "warning", "رسالة من رقم غير مصرح: %s" % sender)
                        continue
                    try:
                        batch = Batch.create({"channel": "whatsapp", "external_message_id": ext_id,
                                              "sender": sender, "subject": "واتساب من %s" % sender})
                        mtype = msg.get("type")
                        if mtype == "text":
                            batch.load_text(rec.body or "")
                        elif mtype in ("image", "document"):
                            media = msg[mtype]
                            content, mime = self.wa_download_media(media["id"])
                            fname = media.get("filename") or "whatsapp_%s.%s" % (
                                ext_id[-8:], (mime or "image/jpeg").split("/")[-1])
                            doc = self.env["alwabrn.source.document"].sudo().create({
                                "name": fname, "datas": base64.b64encode(content), "channel": "whatsapp",
                                "external_ref": ext_id})
                            batch.load_document(doc)
                            if media.get("caption"):
                                rec.body = media["caption"]
                        else:
                            batch.error_message = "نوع رسالة غير مدعوم: %s" % mtype
                        batch.action_validate()
                        rec.write({"batch_id": batch.id, "state": "processed"})
                        created |= batch
                        self.log_event("whatsapp", "info", "دفعة %s من رسالة %s" % (batch.name, ext_id), ok=True)
                    except Exception as e:
                        rec.write({"state": "error", "error": str(e)})
                        self.log_event("whatsapp", "error", "فشل معالجة الرسالة %s: %s" % (ext_id, e))
        return created

    def wa_send_text(self, number, text):
        """Outbound notification through the official API only, and only when
        the admin enabled notifications and the connection is verified."""
        self.ensure_one()
        if self.state != "connected" or not self.wa_notifications:
            self.log_event("whatsapp", "info", "لم يُرسل الإشعار: القناة غير مفعلة/غير متصلة.")
            return False
        token = self._secrets().get("access_token")
        try:
            r = requests.post("%s/%s/messages" % (GRAPH, self.wa_phone_number_id), timeout=30,
                              headers={"Authorization": "Bearer %s" % token},
                              json={"messaging_product": "whatsapp", "to": number, "type": "text",
                                    "text": {"body": text[:4000]}})
            r.raise_for_status()
            self.env["alwabrn.integration.message"].sudo().create({
                "channel": "whatsapp", "direction": "out", "sender": number, "body": text[:5000],
                "external_id": (r.json().get("messages") or [{}])[0].get("id") or "out-%s" % fields.Datetime.now(),
                "state": "processed"})
            return True
        except Exception as e:
            self.log_event("whatsapp", "error", "فشل إرسال إشعار واتساب: %s" % e)
            return False

    @api.model
    def notify(self, text):
        acc = self.sudo().search([("kind", "=", "whatsapp"), ("active", "=", True)], limit=1)
        if not acc:
            return 0
        sent = 0
        for number in [n.strip() for n in (acc.wa_notify_numbers or "").splitlines() if n.strip()]:
            sent += bool(acc.wa_send_text(number, text))
        return sent

    def action_test_connection(self):
        self.ensure_one()
        if self.kind == "whatsapp":
            token = self._secrets().get("access_token")
            if not (token and self.wa_phone_number_id):
                raise UserError("أدخل Phone Number ID ورمز الوصول أولاً.")
            try:
                r = requests.get("%s/%s" % (GRAPH, self.wa_phone_number_id), timeout=30,
                                 headers={"Authorization": "Bearer %s" % token})
                r.raise_for_status()
                self.log_event("whatsapp", "info", "اختبار الاتصال بـ Graph API ناجح.")
            except Exception as e:
                self.log_event("whatsapp", "error", "فشل اختبار الاتصال: %s" % e)
                raise UserError("فشل الاتصال: %s" % e)
        elif self.kind == "email":
            if not self.fetchmail_server_id:
                raise UserError("اختر خادم البريد الوارد أولاً.")
            self.fetchmail_server_id.button_confirm_login()
            self.log_event("email", "info", "اختبار خادم البريد ناجح.", ok=True)
        elif self.kind == "ocr":
            code, st = self.env["alwabrn.ocr.extraction"].provider_status()
            if st != "connected":
                raise UserError("مزود OCR «%s» غير جاهز (%s)." % (code, st))
            self.log_event("ocr", "info", "مزود OCR جاهز: %s" % code, ok=True)
        elif self.kind == "ai":
            key = self._secrets().get("api_key")
            if not key:
                raise UserError("أدخل مفتاح API أولاً.")
            try:
                r = requests.get("https://api.anthropic.com/v1/models", timeout=30,
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
                r.raise_for_status()
                self.log_event("ai", "info", "اختبار مفتاح AI ناجح.", ok=True)
            except Exception as e:
                self.log_event("ai", "error", "فشل اختبار مفتاح AI: %s" % e)
                raise UserError("فشل الاتصال: %s" % e)
        return True

    def action_open_secret_wizard(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.secret.wizard", "view_mode": "form",
                "target": "new", "context": {"default_account_id": self.id}}


class AlwabrnIntegrationMessage(models.Model):
    _name = "alwabrn.integration.message"
    _description = "رسالة تكامل"
    _order = "id desc"

    channel = fields.Selection([("whatsapp", "واتساب"), ("email", "بريد إلكتروني")], required=True, index=True)
    direction = fields.Selection([("in", "واردة"), ("out", "صادرة")], default="in", required=True)
    external_id = fields.Char("معرف الرسالة", required=True, index=True)
    sender = fields.Char("المرسل / المستلم")
    message_date = fields.Datetime("وقت الرسالة", default=fields.Datetime.now)
    body = fields.Text("النص")
    batch_id = fields.Many2one("alwabrn.import.batch", "دفعة الإدخال")
    state = fields.Selection([("received", "مستلمة"), ("processed", "معالجة"), ("unauthorized", "مرسل غير مصرح"),
                              ("error", "خطأ")], default="received", required=True, index=True)
    error = fields.Text("الخطأ")

    _external_uniq = models.Constraint("unique(channel, direction, external_id)", "الرسالة مسجلة مسبقاً.")


class AlwabrnIntegrationLog(models.Model):
    _name = "alwabrn.integration.log"
    _description = "سجل التكامل"
    _order = "id desc"

    account_id = fields.Many2one("alwabrn.integration.account", required=True, ondelete="cascade", index=True)
    level = fields.Selection([("info", "معلومة"), ("warning", "تحذير"), ("error", "خطأ")], required=True)
    message = fields.Text("الرسالة", required=True)
    create_date = fields.Datetime("التوقيت", readonly=True)
