# Encrypted seed data

Files here are encrypted with Fernet (AES-128-CBC + HMAC). They are decrypted only on the
server, with the key in the `ALWABRN_DATA_KEY` environment variable (Render → Environment).
The key is never stored in this repository.

| File | Content |
|---|---|
| `materials_2021.xlsx.enc` | «المواد الموحدة 2021» — 624 materials / 41 categories (contract prices) |
| `stock_count.xlsx.enc` | «حصر مواد الإنشاءات بالمستودع» — original count workbook (kept as source document) |
| `opening_stock.json.enc` | Reviewed opening balance built from the count (1 Drum = 500 m) |

Loaded once at start-up by `deploy/entrypoint-alw.sh` → `hooks.load_seed_files()`; re-runs are no-ops.
