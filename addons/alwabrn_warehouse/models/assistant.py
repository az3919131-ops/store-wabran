import re
from datetime import timedelta

from odoo import api, fields, models

from ..tools.text_parser import SEC_RE, SITE_RE, normalize
from .common import riyadh_day_end_utc, riyadh_day_start_utc, riyadh_today


class AlwabrnAssistant(models.AbstractModel):
    """مساعد الوبران الذكي — deterministic Arabic intent engine.

    Every number in an answer comes from an ORM query executed with the
    current user's access rights. The assistant is read-only by design."""
    _name = "alwabrn.assistant"
    _description = "مساعد الوبران الذكي"

    @api.model
    def ask(self, question):
        q = normalize(question or "").strip()
        low = q.lower()
        if not q:
            return self._reply("اكتب سؤالك، مثال: كم رصيد المادة 908111001؟")
        code = SEC_RE.search(q)
        site = SITE_RE.search(q)
        has = lambda *words: any(w in low for w in words)  # noqa: E731
        if has("قارن") and has("أمس", "امس"):
            return self._compare_yesterday(code.group(1) if code else None)
        if has("لم تتم مطابقت", "غير مطابق", "لم تطابق"):
            return self._not_reconciled()
        if has("فروق", "فرق"):
            return self._discrepancies()
        if has("عهد", "بعهدة") or (site and has("موقع")):
            return self._custody(site.group(1) if site else None, code.group(1) if code else None)
        if has("دخلت", "الوارد", "استلام", "وارد"):
            return self._moves_today(("receipt", "return_in"), "الوارد")
        if has("خرجت", "المنصرف", "صرف", "منصرف"):
            return self._moves_today(("issue_site", "issue", "return_out"), "المنصرف")
        if has("معلق", "اعتماد", "غير معتمد"):
            return self._pending()
        if code and has("رصيد", "كم", "حركة", "كرت"):
            return self._balance(code.group(1))
        if code:
            return self._balance(code.group(1))
        return self._reply(
            "لم أفهم السؤال. أستطيع الإجابة من بيانات النظام الفعلية عن: رصيد مادة بكود SEC، وارد/منصرف اليوم، "
            "عهدة موقع، الفروقات، المواد غير المطابقة، مقارنة رصيد اليوم بالأمس، والاعتمادات المعلقة.")

    def _reply(self, text, columns=None, rows=None, source=None):
        return {"text": text, "columns": columns or [], "rows": rows or [],
                "source": source or "", "generated": fields.Datetime.to_string(fields.Datetime.now())}

    def _product(self, code):
        return self.env["product.template"].search([("alw_sec_code", "=", code)], limit=1)

    def _balance(self, code):
        tmpl = self._product(code)
        if not tmpl:
            return self._reply("الكود %s غير موجود في كتالوج المواد. لن أخمّن مادة بديلة." % code)
        u = tmpl.uom_id.name
        rows = [["المستودع الرئيسي", tmpl.alw_qty_warehouse, u], ["عهد المواقع", tmpl.alw_qty_custody, u],
                ["مواد تالفة", tmpl.alw_qty_damaged, u], ["إجمالي عهدة الشركة", tmpl.alw_qty_total, u]]
        text = "رصيد المادة %s (%s): المستودع %s %s، عهد المواقع %s %s، الإجمالي %s %s." % (
            code, tmpl.name, tmpl.alw_qty_warehouse, u, tmpl.alw_qty_custody, u, tmpl.alw_qty_total, u)
        if tmpl.alw_open_discrepancies:
            text += " يوجد %s فرق مفتوح على هذه المادة." % tmpl.alw_open_discrepancies
        return self._reply(text, ["البيان", "الكمية", "الوحدة"], rows, "stock.quant (أرصدة Odoo الرسمية)")

    def _moves_today(self, types, label):
        today = riyadh_today()
        lines = self.env["alwabrn.transaction.line"].search([
            ("state", "=", "posted"), ("tx_type", "in", types),
            ("date", ">=", riyadh_day_start_utc(today)), ("date", "<=", riyadh_day_end_utc(today))])
        if not lines:
            return self._reply("لا توجد حركات %s مرحّلة اليوم (%s)." % (label, today))
        rows = [[l.sec_code, l.product_id.name, l.qty_product_uom, l.product_id.uom_id.name,
                 l.transaction_id.name, l.site_id.display_name or ""] for l in lines]
        return self._reply("%s اليوم: %s بند في %s حركة." % (label, len(lines), len(lines.transaction_id)),
                           ["كود SEC", "المادة", "الكمية", "الوحدة", "الحركة", "الموقع"], rows,
                           "alwabrn.transaction (مرحّلة عبر stock.picking)")

    def _custody(self, site_code, code):
        dom = [("qty_outstanding", "!=", 0)]
        title = "العهد القائمة بجميع المواقع"
        if site_code:
            site = self.env["alwabrn.site"].search([("code", "=", site_code)], limit=1)
            if not site:
                return self._reply("الموقع %s غير معرف في النظام." % site_code)
            dom.append(("site_id", "=", site.id))
            title = "عهدة الموقع %s" % site.display_name
        if code:
            dom.append(("sec_code", "=", code))
        lines = self.env["alwabrn.custody.line"].search(dom, limit=200)
        if not lines:
            return self._reply("%s: لا توجد عهدة قائمة." % title)
        rows = [[l.site_id.display_name, l.sec_code, l.product_id.name, l.qty_issued, l.qty_returned,
                 l.qty_consumed, l.qty_outstanding, l.uom_id.name] for l in lines]
        return self._reply("%s: %s صنف." % (title, len(lines)),
                           ["الموقع", "كود SEC", "المادة", "مصروف", "مرتجع", "مستهلك", "قائم", "الوحدة"], rows,
                           "stock.move.line (حركات Odoo المنفذة)")

    def _discrepancies(self):
        lines = self.env["alwabrn.reconciliation.line"].search(
            [("status", "=", "diff"), ("resolution", "in", ("open", "adjust_pending"))], limit=200)
        if not lines:
            return self._reply("لا توجد فروقات مفتوحة حالياً.")
        cl = dict(lines._fields["classification"]._description_selection(self.env))
        rows = [[str(l.date), l.sec_code, l.product_id.name, l.odoo_qty,
                 l.book_qty if l.has_book else "غير متوفر", l.physical_qty if l.has_physical else "غير متوفر",
                 l.difference, cl.get(l.classification) or ""] for l in lines]
        return self._reply("يوجد %s فرق مفتوح." % len(lines),
                           ["التاريخ", "كود SEC", "المادة", "Odoo", "الدفتري", "الفعلي", "الفرق", "التصنيف المقترح"],
                           rows, "alwabrn.reconciliation.line")

    def _not_reconciled(self):
        since = riyadh_today() - timedelta(days=30)
        done = self.env["alwabrn.reconciliation.line"].search([("date", ">=", since)]).mapped("product_id")
        bal = self.env["alwabrn.stock.balance"].search([("role", "=", "warehouse")])
        missing = bal.filtered(lambda b: b.product_id not in done)
        if not missing:
            return self._reply("كل المواد ذات الرصيد تمت مطابقتها خلال آخر 30 يوماً.")
        rows = [[b.sec_code, b.product_id.name, b.quantity, b.uom_id.name] for b in missing[:200]]
        return self._reply("%s مادة لها رصيد بالمستودع ولم تدخل أي مطابقة خلال آخر 30 يوماً." % len(missing),
                           ["كود SEC", "المادة", "رصيد Odoo", "الوحدة"], rows, "stock.quant / المطابقات")

    def _compare_yesterday(self, code):
        svc = self.env["alwabrn.stock.service"]
        today = riyadh_today()
        wh = svc.main_stock_location()
        products = self._product(code).product_variant_id if code else None
        if code and not products:
            return self._reply("الكود %s غير موجود في كتالوج المواد." % code)
        now = svc.balance_at(wh, riyadh_day_end_utc(today), products)
        prev = svc.balance_at(wh, riyadh_day_end_utc(today - timedelta(days=1)), products)
        rows = []
        for p in self.env["product.product"].browse(list(set(now) | set(prev))):
            a, b = prev.get(p.id, 0.0), now.get(p.id, 0.0)
            if abs(a - b) > 1e-9 or code:
                rows.append([p.alw_sec_code, p.name, a, b, b - a, p.uom_id.name])
        if not rows:
            return self._reply("لا يوجد تغير في أرصدة المستودع بين الأمس واليوم.")
        return self._reply("مقارنة رصيد المستودع: الأمس مقابل اليوم — %s مادة تغير رصيدها." % len(rows),
                           ["كود SEC", "المادة", "رصيد الأمس", "رصيد اليوم", "التغير", "الوحدة"], rows,
                           "stock.move.line حتى نهاية كل يوم (توقيت الرياض)")

    def _pending(self):
        tx = self.env["alwabrn.transaction"].search_count([("state", "in", ("draft", "pending"))])
        b = self.env["alwabrn.import.batch"].search_count([("state", "in", ("draft", "needs_review", "validated"))])
        return self._reply("بانتظار الاعتماد: %s حركة مواد و %s دفعة إدخال. الاعتماد يتم من المدير فقط — المساعد لا يعتمد ولا يعدل المخزون." % (tx, b))
