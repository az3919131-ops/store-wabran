"""Parser for the «أوامر عمل المشاريع» workbook (xls/xlsx).

Each sheet is a stage of the work order life-cycle. The header row is found by
the cell «رقم أمر العمل»; columns are mapped by header text, never by position."""
import datetime
import io
import re

STAGES = [
    ("delivered", "تم التسليم للمقاول (لم يُستلم)", ["تم التسليم", "تم تسليم"]),
    ("received", "تم الاستلام من المقاول", ["تم الاستلام"]),
    ("drawing", "مؤكد — باقي الرسم الهندسي", ["الرسم"]),
    ("extract", "دخلت مستخلص ولم تصرف", ["مستخلص"]),
]
STAGE_SEL = [(k, v) for k, v, _ in STAGES] + [("minutes", "من منصة المحاضر"), ("manual", "إدخال يدوي")]

HEAD = {
    "wo": ["رقم أمر العمل"],
    "type_code": ["رمز نوع أمر العمل"],
    "type_name": ["نوع امر العمل", "نوع أمر العمل"],
    "delivery_date": ["تاريخ التسليم للمقاول"],
    "receipt_date": ["تاريخ الاستلام من المقاول"],
    "labor_cost": ["تكلفة العمالة"],
    "extract_no": ["رقم المستخلص"],
    "extract_date": ["تاريخ المستخلص"],
    "completion_date": ["تاريخ شهادة الانجاز", "تاريخ شهادة الإنجاز"],
    "completion_amount": ["مبلغ شهادة"],
    "status_note": ["الموقف", "ملاحظة"],
    "responsible": ["المسؤل", "المسؤول"],
    "plan": ["الخطة"],
    "location_details": ["تفاصيل الموقع"],
    "consultant": ["الاستشاري"],
    "license": ["الرخصة"],
    "site_text": ["الموقع"],
}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").replace("​", "")).strip()


def stage_of(sheet_name):
    n = _norm(sheet_name)
    for code, _label, keys in STAGES:
        if any(k in n for k in keys):
            return code
    return None


def _cell_date(v, datemode=0):
    if v in (None, ""):
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return (datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(v))).date()
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(v))
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return float(m.group(0)) if m else None


def _wo(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    s = re.sub(r"\s+", "", str(v or ""))
    return s if re.fullmatch(r"\d{6,14}", s) else None


def read_sheets(content, filename):
    """Return [(sheet_name, [row values])] for xls or xlsx."""
    if (filename or "").lower().endswith(".xls"):
        import xlrd
        book = xlrd.open_workbook(file_contents=content)
        return [(sh.name, [sh.row_values(r) for r in range(sh.nrows)]) for sh in book.sheets()]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]


def parse(sheets):
    """Return (items, problems). items: dicts per (wo, type_code) row."""
    items, problems = [], []
    for name, rows in sheets:
        stage = stage_of(name)
        hdr_i, cols = None, {}
        for i, row in enumerate(rows[:15]):
            texts = [_norm(c) for c in row]
            if any(t.startswith("رقم أمر العمل") for t in texts):
                hdr_i = i
                for j, t in enumerate(texts):
                    for field, keys in HEAD.items():
                        if field in cols:
                            continue
                        if field == "site_text" and t != "الموقع":
                            continue
                        if any(t.startswith(k) or t == k for k in keys):
                            cols[field] = j
                            break
                break
        if hdr_i is None:
            problems.append("ورقة «%s»: لم يُعثر على صف العناوين (رقم أمر العمل)" % _norm(name))
            continue
        if not stage:
            problems.append("ورقة «%s»: مرحلة غير معروفة — استوردت كمرحلة يدوية" % _norm(name))
        for i, row in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
            get = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else None  # noqa: E731
            wo = _wo(get("wo"))
            if not wo:
                if any(c not in (None, "") for c in row):
                    problems.append("ورقة «%s» صف %s: رقم أمر عمل غير صالح «%s»" % (_norm(name), i, get("wo")))
                continue
            code = get("type_code")
            code = str(int(code)) if isinstance(code, float) and code == int(code) else _norm(code)
            items.append({
                "wo": wo, "type_code": code or "", "type_name": _norm(get("type_name")), "stage": stage or "manual",
                "sheet": _norm(name), "row": i,
                "delivery_date": _cell_date(get("delivery_date")), "receipt_date": _cell_date(get("receipt_date")),
                "completion_date": _cell_date(get("completion_date")), "extract_date": _cell_date(get("extract_date")),
                "labor_cost": _num(get("labor_cost")), "completion_amount": _num(get("completion_amount")),
                "extract_no": _norm(get("extract_no")).replace(".0", "") if get("extract_no") not in (None, "") else "",
                "status_note": _norm(get("status_note")), "responsible": _norm(get("responsible")),
                "plan": _norm(get("plan")), "location_details": _norm(get("location_details")),
                "consultant": _norm(get("consultant")), "license": _norm(get("license")), "site_text": _norm(get("site_text")),
            })
    return items, problems
