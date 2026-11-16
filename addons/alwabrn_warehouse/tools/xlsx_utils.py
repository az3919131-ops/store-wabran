import csv
import io

import openpyxl

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Neutralise spreadsheet formula injection for text cells."""
    if isinstance(value, str) and value and value[0] in FORMULA_PREFIXES:
        return "'" + value
    return value


def read_table(content, filename):
    """Read XLSX/XLS/CSV into list of rows (list of values). First sheet only."""
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        for enc in ("utf-8-sig", "cp1256", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sample = text[:2000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect)]
    if name.endswith(".xls"):
        try:
            import xlrd
        except ImportError as e:  # pragma: no cover
            raise ValueError("xlrd is required for .xls files") from e
        book = xlrd.open_workbook(file_contents=content)
        sh = book.sheet_by_index(0)
        return [sh.row_values(i) for i in range(sh.nrows)]
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


HEADER_HINTS = {
    "sec_code": ["sec", "كود", "رمز", "code", "رقم المادة", "item"],
    "quantity": ["qty", "quantity", "الكمية", "كمية", "رصيد", "balance", "العدد", "count"],
    "unit": ["unit", "uom", "الوحدة", "وحدة"],
    "description": ["desc", "الوصف", "وصف", "البيان", "اسم", "material", "المادة"],
    "date": ["date", "التاريخ", "تاريخ"],
    "site": ["site", "الموقع", "موقع", "location"],
    "document": ["doc", "مستند", "سند", "reference", "مرجع"],
}


def detect_columns(rows, max_scan=15):
    """Return (header_row_index, {field: col_index}). Falls back to content sniffing."""
    best, best_map = -1, {}
    for i, row in enumerate(rows[:max_scan]):
        mapping = {}
        for j, cell in enumerate(row):
            if not isinstance(cell, str):
                continue
            low = cell.strip().lower()
            for field, hints in HEADER_HINTS.items():
                if field in mapping:
                    continue
                if any(h in low for h in hints):
                    # 'sec #' should not also be matched as description
                    mapping[field] = j
                    break
        if len(mapping) > len(best_map):
            best, best_map = i, mapping
    if "sec_code" not in best_map:
        # sniff: column where most values look like 9 digit codes
        scores = {}
        for row in rows[:200]:
            for j, cell in enumerate(row):
                s = str(int(cell)) if isinstance(cell, float) and cell == int(cell) else str(cell or "").strip()
                if s.isdigit() and len(s) == 9:
                    scores[j] = scores.get(j, 0) + 1
        if scores:
            best_map["sec_code"] = max(scores, key=scores.get)
    if "quantity" not in best_map and "sec_code" in best_map:
        scores = {}
        for row in rows[:200]:
            for j, cell in enumerate(row):
                if j != best_map["sec_code"] and isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    scores[j] = scores.get(j, 0) + 1
        if scores:
            best_map["quantity"] = max(scores, key=scores.get)
    return best, best_map
