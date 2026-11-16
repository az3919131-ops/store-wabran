import base64
import hashlib
import io
import logging
import shutil
import subprocess
import tempfile

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

MAX_FILE_MB = 20
ALLOWED_MIMES = {
    "image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel",
    "text/csv", "text/plain",
}
CHANNELS = [
    ("manual", "إدخال يدوي"), ("paste", "لصق بيانات"), ("camera", "كاميرا الجوال"),
    ("image", "رفع صورة"), ("pdf", "ملف PDF"), ("excel", "ملف Excel"),
    ("whatsapp", "واتساب"), ("email", "بريد إلكتروني"),
]
LOW_CONFIDENCE = 60.0


class AlwabrnSourceDocument(models.Model):
    _name = "alwabrn.source.document"
    _description = "مستند مصدر"
    _order = "id desc"

    name = fields.Char("اسم الملف", required=True)
    datas = fields.Binary("الملف", attachment=True, required=True)
    mimetype = fields.Char("نوع الملف", readonly=True)
    file_size = fields.Integer("الحجم (بايت)", readonly=True)
    file_hash = fields.Char("بصمة الملف SHA-256", readonly=True, index=True)
    channel = fields.Selection(CHANNELS, "قناة الورود", required=True, default="image", index=True)
    register_type = fields.Selection([
        ("incoming", "دفتر الوارد"), ("outgoing", "دفتر المنصرف"), ("daily_balance", "كشف الرصيد اليومي"),
        ("physical", "كشف الجرد الفعلي"), ("returns", "دفتر المرتجعات"), ("other", "أخرى"),
    ], "نوع الدفتر", default="other")
    uploaded_by = fields.Many2one("res.users", "رفع بواسطة", default=lambda s: s.env.user, readonly=True)
    batch_id = fields.Many2one("alwabrn.import.batch", "دفعة الإدخال", index=True, ondelete="set null")
    extraction_ids = fields.One2many("alwabrn.ocr.extraction", "document_id", "نتائج الاستخراج")
    duplicate_of_id = fields.Many2one("alwabrn.source.document", "مكرر للمستند", readonly=True)
    is_image = fields.Boolean(compute="_compute_is_image")
    external_ref = fields.Char("مرجع خارجي (رسالة)", index=True, readonly=True)

    @api.depends("mimetype")
    def _compute_is_image(self):
        for rec in self:
            rec.is_image = (rec.mimetype or "").startswith("image/")

    @api.model_create_multi
    def create(self, vals_list):
        from odoo.tools.mimetypes import guess_mimetype
        for vals in vals_list:
            raw = base64.b64decode(vals.get("datas") or b"")
            if not raw:
                raise ValidationError("الملف فارغ.")
            if len(raw) > MAX_FILE_MB * 1024 * 1024:
                raise ValidationError("حجم الملف يتجاوز الحد المسموح (%s م.ب)." % MAX_FILE_MB)
            mime = guess_mimetype(raw, default="application/octet-stream")
            name = (vals.get("name") or "").lower()
            if mime in ("application/octet-stream", "text/plain", "application/zip"):
                if name.endswith(".csv"):
                    mime = "text/csv"
                elif name.endswith(".xlsx"):
                    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                elif name.endswith((".heic", ".heif")):
                    mime = "image/heic"
            if mime not in ALLOWED_MIMES:
                raise ValidationError("نوع الملف غير مسموح: %s" % mime)
            vals.update(mimetype=mime, file_size=len(raw), file_hash=hashlib.sha256(raw).hexdigest())
        docs = super().create(vals_list)
        for doc in docs:
            dup = self.search([("file_hash", "=", doc.file_hash), ("id", "<", doc.id)], order="id", limit=1)
            if dup:
                doc.duplicate_of_id = dup
        return docs

    def _raw(self):
        self.ensure_one()
        return base64.b64decode(self.with_context(bin_size=False).datas)

    def action_run_ocr(self):
        for doc in self:
            self.env["alwabrn.ocr.extraction"].run(doc)
        return True


class AlwabrnOcrExtraction(models.Model):
    _name = "alwabrn.ocr.extraction"
    _description = "نتيجة استخراج OCR"
    _order = "id desc"

    document_id = fields.Many2one("alwabrn.source.document", required=True, ondelete="cascade", index=True)
    provider = fields.Char("المزود", readonly=True)
    page = fields.Integer("الصفحة", default=1, readonly=True)
    state = fields.Selection([("done", "تم"), ("failed", "فشل"), ("unavailable", "المزود غير مهيأ")],
                             readonly=True, required=True)
    raw_text = fields.Text("النص المستخرج", readonly=True)
    confidence = fields.Float("متوسط الثقة %", readonly=True)
    line_data = fields.Json("بيانات الأسطر", readonly=True)
    error = fields.Text("الخطأ", readonly=True)

    # ------------------------------------------------------------ providers
    @api.model
    def provider_code(self):
        return self.env["ir.config_parameter"].sudo().get_param("alwabrn.ocr_provider", "tesseract")

    @api.model
    def provider_status(self):
        """Honest availability status used by the integration settings page."""
        code = self.provider_code()
        if code == "tesseract":
            ok = bool(shutil.which("tesseract"))
            try:
                import pytesseract  # noqa: F401
            except ImportError:
                ok = False
            return code, ("connected" if ok else "pending_config")
        if code == "anthropic":
            key = self.env["alwabrn.integration.account"].sudo().get_secret("ai", "api_key")
            return code, ("auth_required" if key else "pending_config")
        return code, "disconnected"

    @api.model
    def run(self, document):
        """Run OCR. Returns extraction records (one per page). Never raises for
        provider failures: a failed extraction is stored and shown to the user."""
        self = self.sudo()  # extraction records are system-written, read-only for users
        code = self.provider_code()
        try:
            if document.mimetype == "application/pdf":
                pages = self._pdf_pages(document._raw())
            elif (document.mimetype or "").startswith("image/"):
                pages = [("image", document._raw())]
            else:
                raise UserError("OCR يدعم الصور وملفات PDF فقط.")
            results = self.browse()
            for idx, (kind, payload) in enumerate(pages, start=1):
                if kind == "text":
                    lines = [{"text": t, "conf": 100.0} for t in payload.splitlines() if t.strip()]
                    vals = {"provider": "pdf-text-layer", "state": "done"}
                else:
                    lines = getattr(self, "_ocr_%s" % code)(payload, document.mimetype)
                    vals = {"provider": code, "state": "done"}
                confs = [l["conf"] for l in lines if l.get("conf") is not None]
                vals.update({
                    "document_id": document.id, "page": idx, "line_data": lines,
                    "raw_text": "\n".join(l["text"] for l in lines),
                    "confidence": sum(confs) / len(confs) if confs else 0.0,
                })
                results |= self.create(vals)
            return results
        except Exception as e:  # provider failure is data, not a crash
            _logger.warning("OCR failed for document %s: %s", document.id, e)
            unavailable = isinstance(e, (ImportError, AttributeError, FileNotFoundError)) or "not configured" in str(e)
            return self.create({"document_id": document.id, "provider": code,
                                "state": "unavailable" if unavailable else "failed", "error": str(e)})

    def _pdf_pages(self, raw):
        """Text layer first (exact); otherwise rasterise pages for OCR."""
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(raw)
            f.flush()
            if shutil.which("pdftotext"):
                txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, timeout=120).stdout
                pages = [p for p in txt.decode("utf-8", "ignore").split("\f")]
                if any(len(p.strip()) > 20 for p in pages):
                    return [("text", p) for p in pages if p.strip()]
            from pdf2image import convert_from_path
            out = []
            for img in convert_from_path(f.name, dpi=200):
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                out.append(("image", buf.getvalue()))
            return out

    def _ocr_tesseract(self, raw, mimetype):
        import pytesseract
        from PIL import Image, ImageOps
        if "hei" in (mimetype or ""):
            from pillow_heif import register_heif_opener  # optional dependency for iPhone HEIC photos
            register_heif_opener()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("L")
        if max(img.size) < 1600:
            ratio = 1600 / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
        data = pytesseract.image_to_data(img, lang="ara+eng", config="--psm 6",
                                         output_type=pytesseract.Output.DICT)
        grouped = {}
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            g = grouped.setdefault(key, {"words": [], "confs": []})
            g["words"].append(word)
            conf = float(data["conf"][i])
            if conf >= 0:
                g["confs"].append(conf)
        return [{"text": " ".join(g["words"]),
                 "conf": (min(g["confs"]) if g["confs"] else 0.0)} for g in grouped.values()]

    def _ocr_anthropic(self, raw, mimetype):
        """AI vision provider (optional). Requires an API key configured by the admin."""
        Account = self.env["alwabrn.integration.account"].sudo()
        key = Account.get_secret("ai", "api_key")
        if not key:
            raise UserError("AI vision provider not configured")
        model = self.env["ir.config_parameter"].sudo().get_param("alwabrn.ai_model", "claude-sonnet-5-5")
        prompt = (
            "You transcribe a warehouse register photo. Output ONLY the table rows, one per line, as: "
            "SEC_CODE<TAB>QUANTITY<TAB>UNIT<TAB>DESCRIPTION<TAB>CONFIDENCE(0-100). "
            "If a value is unreadable write ? for it and give low confidence. Never guess or invent digits.")
        resp = requests.post(
            "https://api.anthropic.com/v1/messages", timeout=120,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mimetype,
                                             "data": base64.b64encode(raw).decode()}},
                {"type": "text", "text": prompt}]}]})
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", []))
        lines = []
        for row in text.splitlines():
            if not row.strip():
                continue
            parts = row.split("\t")
            try:
                conf = float(parts[-1]) if len(parts) > 1 else 50.0
                body = " ".join(parts[:-1])
            except ValueError:
                conf, body = 50.0, " ".join(parts)
            if "?" in body:
                conf = min(conf, 30.0)
            lines.append({"text": body, "conf": conf})
        return lines

    def _ocr_none(self, raw, mimetype):
        raise UserError("OCR provider not configured")
