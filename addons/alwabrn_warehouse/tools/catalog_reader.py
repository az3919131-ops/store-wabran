"""Reader for the SEC unified materials workbook (sheet «نجران»).

Returns raw records + validation issues. Nothing is repaired automatically.
"""
import io
from collections import Counter

import openpyxl

KNOWN_UNITS = ("EA", "KIT", "KM", "M", "R", "DRUM")
MAIN_SHEET = "نجران"


def read_catalog(content):
    wb = openpyxl.load_workbook(io.BytesIO(content))  # keep formulas
    ws = wb[MAIN_SHEET] if MAIN_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
    records, categories = [], []
    category = None
    blank_since_category = False
    for r in range(3, ws.max_row + 1):
        no, desc, sec, unit, price, extra = [ws.cell(r, c).value for c in range(2, 8)]
        if isinstance(no, str) and desc is None and sec is None:
            category = no.strip()
            categories.append(category)
            blank_since_category = False
            continue
        if desc is None and sec is None:
            if records and records[-1]["category"] == category:
                blank_since_category = True
            continue
        issues = []
        sec_str = str(sec).strip() if sec is not None else ""
        if isinstance(sec, float) and sec == int(sec):
            sec_str = str(int(sec))
        if not sec_str:
            issues.append(("error", "missing_sec", "كود SEC مفقود"))
        elif not (sec_str.isdigit() and len(sec_str) == 9):
            issues.append(("warning", "sec_format", "كود SEC «%s» لا يطابق الصيغة (9 أرقام) ويحتاج تحقق" % sec_str))
        if not desc or not str(desc).strip():
            issues.append(("error", "missing_desc", "الوصف مفقود"))
        unit_str = str(unit).strip().upper() if unit else ""
        if unit_str not in KNOWN_UNITS:
            issues.append(("error", "bad_unit", "وحدة غير معروفة «%s»" % (unit or "")))
        if price is not None and not isinstance(price, (int, float)):
            issues.append(("warning", "bad_price", "سعر غير رقمي"))
        rec_category = category
        if blank_since_category:
            # record sits after a blank separator without its own category header
            issues.append(("warning", "category_unverified",
                           "السجل يقع بعد فاصل فارغ دون عنوان فئة (آخر فئة: %s) — الفئة تحتاج تحقق" % category))
            rec_category = None
        records.append({
            "row": r, "no": no, "description": str(desc) if desc is not None else "",
            "sec_code": sec_str, "unit": unit_str, "price": price if isinstance(price, (int, float)) else 0.0,
            "extra_formula": extra if isinstance(extra, str) else None,
            "category": rec_category, "positional_category": category, "issues": issues,
        })
    counts = Counter(r["sec_code"] for r in records if r["sec_code"])
    for rec in records:
        if rec["sec_code"] and counts[rec["sec_code"]] > 1:
            rec["issues"].append(("error", "duplicate", "كود SEC مكرر داخل الملف"))
    reference_rows = []
    for name in wb.sheetnames:
        if name == ws.title:
            continue
        for row in wb[name].iter_rows(values_only=True):
            vals = [v for v in row if v is not None]
            if vals:
                reference_rows.append({"sheet": name, "values": vals,
                                       "already_in_main": any(str(v) in counts for v in vals)})
    return {"records": records, "categories": categories, "reference_rows": reference_rows,
            "sheet": ws.title}
