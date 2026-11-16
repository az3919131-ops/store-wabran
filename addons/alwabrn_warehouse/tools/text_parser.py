"""Pure-python parser for pasted / OCR / WhatsApp / e-mail inventory text.

It never invents data: a line without a recognisable SEC code is returned
with ``sec_code=None`` and an explicit issue so that a human reviews it.
"""
import re
from datetime import date

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")

UNIT_ALIASES = {
    "EA": ["ea", "each", "pcs", "pc", "قطعة", "قطع", "حبة", "حبه", "عدد"],
    "KM": ["km", "كم", "كيلومتر", "كيلو متر"],
    "M": ["m", "mtr", "meter", "meters", "متر", "م", "أمتار", "امتار"],
    "KIT": ["kit", "kits", "طقم", "أطقم", "اطقم"],
    "R": ["r", "roll", "rolls", "لفة", "لفه", "لفات", "رول"],
    "DRUM": ["drum", "drums", "برميل", "براميل", "بكرة", "بكره"],
}
_UNIT_LOOKUP = {a.lower(): code for code, als in UNIT_ALIASES.items() for a in als}

# keyword -> transaction type (order matters: most specific first)
TYPE_KEYWORDS = [
    ("return_in", ["ارتجاع", "إرجاع", "ارجاع", "مرتجع", "رجيع", "returned", "return"]),
    ("consume", ["استهلاك", "استهلك", "تركيب", "تم تركيب", "consumed", "installed"]),
    ("damaged", ["تالف", "تالفة", "damaged"]),
    ("scrap", ["سكراب", "خردة", "scrap"]),
    ("receipt", ["استلام", "استلمنا", "تم توريد", "توريد", "وارد", "received", "receipt"]),
    ("issue", ["صرف", "تم صرف", "منصرف", "إخراج", "اخراج", "issued", "issue"]),
    ("transfer", ["تحويل", "نقل", "transfer"]),
    ("balance", ["رصيد", "الرصيد", "balance"]),
    ("count", ["جرد", "عد فعلي", "physical count", "count"]),
]

SEC_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")
NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])")
DATE_RES = [
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)"), "dmy"),
]
SITE_RE = re.compile(r"(?:موقع|الموقع|site)\s*[:#-]?\s*([\w\-/]+)", re.I)
WO_RE = re.compile(r"(?:أمر عمل|امر عمل|أمر العمل|work\s*order|w\.?o\.?)\s*[:#-]?\s*([\w\-/]+)", re.I)
DOC_RE = re.compile(r"(?:مستند|سند|إذن|اذن|doc(?:ument)?)\s*(?:رقم|no\.?)?\s*[:#-]?\s*([\w\-/]+)", re.I)


def normalize(text):
    return (text or "").translate(AR_DIGITS).replace("‏", "").replace("‎", "")


def detect_unit(token):
    return _UNIT_LOOKUP.get((token or "").strip().lower().strip(".,:;()"))


def detect_type(text):
    low = text.lower()
    for code, words in TYPE_KEYWORDS:
        for w in words:
            if w in low:
                return code
    return None


def _to_float(s):
    return float(s.replace(",", ""))


def parse_date(text):
    for rx, kind in DATE_RES:
        m = rx.search(text)
        if m:
            try:
                if kind == "ymd":
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.span()
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1))), m.span()
            except ValueError:
                continue
    return None, None


def parse_line(raw):
    """Parse one line. Returns dict or None for empty lines."""
    line = normalize(raw).strip()
    if not line:
        return None
    res = {
        "raw_text": raw.strip(), "sec_code": None, "quantity": None, "unit": None,
        "date": None, "detected_type": detect_type(line), "site_ref": None,
        "work_order_ref": None, "document_ref": None, "description": None, "issues": [],
    }
    work = line
    d, span = parse_date(work)
    if d:
        res["date"] = d
        work = work[:span[0]] + " " + work[span[1]:]
    for key, rx in (("site_ref", SITE_RE), ("work_order_ref", WO_RE), ("document_ref", DOC_RE)):
        m = rx.search(work)
        if m:
            res[key] = m.group(1)
            work = work[:m.start()] + " " + work[m.end():]
    codes = SEC_RE.findall(work)
    if len(codes) == 1:
        res["sec_code"] = codes[0]
    elif len(codes) > 1:
        res["sec_code"] = codes[0]
        res["issues"].append("multiple_codes")
    else:
        res["issues"].append("no_sec_code")
    work_wo_code = SEC_RE.sub(" ", work)
    # unit detection: token adjacent to numbers or standalone
    tokens = re.split(r"[\s\t|;]+", work_wo_code)
    for tok in tokens:
        u = detect_unit(tok)
        if u and tok.strip().lower() not in ("عدد", "م", "m", "r"):
            res["unit"] = u
            break
    if not res["unit"]:
        m = re.search(r"\d\s*(km|m|كم|م|متر)\b", work_wo_code, re.I)
        if m:
            res["unit"] = detect_unit(m.group(1))
        else:
            for tok in tokens:
                if tok.strip().lower() in ("m", "r", "م") and len(tokens) <= 6:
                    res["unit"] = detect_unit(tok)
                    break
    nums = [_to_float(n) for n in NUM_RE.findall(work_wo_code)]
    if len(nums) == 1:
        res["quantity"] = nums[0]
    elif len(nums) > 1:
        res["quantity"] = nums[0]
        res["issues"].append("multiple_numbers")
    else:
        res["issues"].append("no_quantity")
    desc = re.sub(r"[\d.,]+", " ", work_wo_code)
    desc = re.sub(r"\s+", " ", desc).strip(" -:|\t")
    res["description"] = desc or None
    return res


def parse_text(text):
    """Parse a multi-line block; returns list of parsed line dicts."""
    out = []
    for i, raw in enumerate((text or "").splitlines(), start=1):
        parsed = parse_line(raw)
        if parsed is None:
            continue
        # skip pure header lines (no digits at all)
        if parsed["sec_code"] is None and parsed["quantity"] is None:
            continue
        parsed["line_no"] = i
        out.append(parsed)
    return out
