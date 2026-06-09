# ABN AMRO `.TAB` transaction import

**Date:** 2026-06-09
**Status:** Approved (brainstorm), ready to implement
**Scope:** Add a new bank-file parser for the ABN AMRO tab-separated (`.TAB`) export, wired through the existing preview → confirm → reconcile pipeline. `.TAB` import only this session; reconciliation is the already-shipped L3.2 flow (no new work).

## Problem

ABN AMRO's only transaction export for some account flows is the `.TAB` (tab-separated) format. The import system today accepts CSV (ING) and OFX/QFX only, so an ABN `.TAB` upload fails. A friend testing the app hit this.

## File format (observed)

Tab-separated, **no header row**, exactly 8 fields per line. Sample line:

```
845455273<TAB>EUR<TAB>20260220<TAB>43,33<TAB>31,62<TAB>20260220<TAB>-11,71<TAB>/TRTP/SEPA Incasso.../NAME/VITENS NV/.../IBAN/NL94INGB0000869000/...
```

| # | ABN field | Maps to |
|---|---|---|
| 0 | Account number | ignored (user picks account in UI); retained in `raw_data` |
| 1 | Currency (`EUR`) | `raw_data` (informational; no blocking on mismatch) |
| 2 | Transaction (book) date `YYYYMMDD` | `ParsedRow.date` (reuse `_parse_date_yyyymmdd`) |
| 3 | Balance before | `raw_data` |
| 4 | Balance after | `raw_data` |
| 5 | Value date `YYYYMMDD` | `raw_data` |
| 6 | Signed amount (`-11,71` debit / `15,75` credit) | sign → `type` (`expense`/`income`); `abs` → `ParsedRow.amount` |
| 7 | Description (rich; see below) | parsed → `description` + `counterparty` |

**Balance columns are intentionally not used for transaction data.** In real exports they do not form a reliable running balance (consecutive rows do not reconcile) — a known ABN quirk. Retained only in `raw_data`.

**Amount sign:** parse the Decimal (may be negative). `type = "expense"` if `value < 0` else `"income"`; `amount = abs(value)`. The confirm schema requires `amount > 0`, which abs satisfies.

**Encoding:** decode bytes as UTF-8; on `UnicodeDecodeError` fall back to `cp1252` (Dutch names/accents). Strip BOM via existing `_strip_bom`.

## Rich description parser

Pure, independently testable function in the new module:

```
parse_abn_description(raw: str) -> tuple[str, str | None, dict]
# returns (description, counterparty, extracted_tags)
```

Three branches:

1. **SEPA** — raw starts with `/TRTP/`. Split on `/` into `TAG`/`value` pairs (tags seen: `TRTP`, `CSID`, `NAME`, `MARF`, `REMI`, `IBAN`, `BIC`, `EREF`, `ULTD`, `ORDP`, `ID`). Then:
   - `counterparty = value of NAME` (trimmed), else `None`.
   - `description = NAME` when present (clean, best for auto-categorization), else the whitespace-collapsed raw string.
   - All parsed tags go into `extracted_tags` (and thus `raw_data`) so `REMI`/`EREF`/`IBAN` detail is never lost.

2. **POS/ATM** — raw starts with `BEA, ` or `GEA, `. Split on runs of 2+ spaces into segments:
   `["BEA, Betaalpas", "DEKAMARKT LOC 529,PAS523", "NR:..., 20.02.26/18:57", "AMERSFOORT"]`.
   - `merchant = segment[1]` with the trailing `,PASxxx` card suffix stripped.
   - `counterparty = merchant`; `description = merchant`.
   - `BEA` = card/POS payment, `GEA` = ATM withdrawal. Remaining segments (card NR, datetime, city) → `extracted_tags`.

3. **Fallback** — anything else: `description = whitespace-collapsed raw`, `counterparty = None`.

> Design decision (user-approved): `description` carries the clean counterparty name, **not** the remittance/invoice text. Full detail is preserved in `raw_data` and visible on the reconcile row. Rationale: cleaner display + stronger deterministic auto-categorization (L3.10 normalizes the description into a merchant key).

## Backend wiring

New module `backend/app/services/import_abn_tab.py`:
- `parse_tab(content: str) -> list[ParsedRow]`.
- Reuses `_parse_amount`, `_parse_date_yyyymmdd`, `_strip_bom`, `ParseError` from `import_parser.py` (import them; do not duplicate).
- Splits into lines; skips blank lines. A non-blank line that does not have exactly 8 tab fields → `ParseError(..., row_number=i)` (mirrors CSV behavior).
- Enforce **`MAX_ROWS = 2000`** (raise `ParseError` past the cap — matches the #417 OFX DoS posture). Pure line iteration, fast: no async executor / timeout needed.
- Empty file (no transaction rows) → `ParseError`.

New endpoint in `import_router.py`:
- `POST /api/v1/import/tab/preview` — mirrors `/preview` (CSV): read file (reuse the existing `MAX_UPLOAD_BYTES = 5 MB` guard), decode (UTF-8 → cp1252 fallback), call `parse_tab`, then `import_service.build_preview(..., source_format="tab")`. Same `ParseError → ValidationError` and `MissingCategoryTypeError → 400` translation as the CSV path.

Enum / Literal updates (all three sites — they must agree by construction):
- `backend/app/models/import_batch.py` → `ImportSourceFormat.TAB = "tab"`.
- `backend/app/schemas/import_reconciliation.py` → its `ImportSourceFormat` wire enum `+ TAB = "tab"`.
- `backend/app/schemas/import_schemas.py` → `ImportConfirmRequest.source_format: Literal["csv", "ofx", "tab"]`.

The confirm / `build_preview` / `execute_import` / reconciliation services are format-agnostic — **no changes** beyond the enum/Literal.

## Migration (REQUIRED — prod-only landmine)

`import_batches.source_format` is a **native MySQL `ENUM('csv','ofx')`** column (SQLAlchemy `Enum` with `values_callable`). Adding `TAB` to the Python enum is NOT enough: a `.TAB` confirm would pass CI (SQLite test DBs are built from the models via `create_all`, so they pick up the new value) but **500 on production MySQL** when inserting the `import_batches` row, because MySQL rejects an out-of-set ENUM value. The preview endpoint is unaffected (in-memory); only confirm writes the column.

New Alembic migration:
- `down_revision` = current head (find it: `ls backend/alembic/versions` or `alembic heads`; do NOT hardcode — the head has advanced well past the numbers quoted in older specs).
- Upgrade: `ALTER TABLE import_batches MODIFY COLUMN source_format ENUM('csv','ofx','tab') NOT NULL;` (guard for the MySQL dialect; no-op on SQLite).
- Downgrade: revert to `ENUM('csv','ofx')` (pre-launch, fine to assume no `tab` rows exist).
- Verify the exact existing column DDL first (`SHOW COLUMNS FROM import_batches LIKE 'source_format'` or read the migration that created it) so the `MODIFY` matches NOT NULL / default exactly.

## Frontend wiring (minimal)

`frontend/app/import/page.tsx`:
- File picker `accept=".csv,.tab,.TAB"`.
- On file select, choose the preview endpoint by extension: name ends with `.tab` (case-insensitive) → `/api/v1/import/tab/preview`; else `/api/v1/import/preview`.
- Confirm already echoes `source_format` from the preview response — no change.
- OFX flow untouched.
- Check `frontend/lib/types.ts` (and any UI that renders a source-format pill) for a `source_format` union — add `"tab"` if one exists.

## Tests

- `backend/tests/services/test_import_abn_tab.py` — parser unit tests: 8-column mapping; sign → `type`; SEPA `NAME` → counterparty; POS `BEA`/`GEA` merchant extraction + `,PASxxx` strip; fallback branch; malformed line (wrong field count) raises with `row_number`; European amount + `YYYYMMDD` date; cp1252 decode; `MAX_ROWS` cap; empty file.
- `backend/tests/routers/test_import_tab.py` — preview endpoint: happy path against the fixture; auth required; org-scoping; oversize/row-cap rejection; bad file → 400. Mirror `test_import_ofx.py` / `test_import_contracts.py`.
- Fixture: `backend/tests/fixtures/import/tab/abn_sample.tab` — **synthetic, anonymized** (fabricated IBANs/names/amounts). Do NOT commit the real `extrato.TAB`.
- Frontend: extend `frontend/tests/app/import-page.test.tsx` to assert a `.tab` file routes to the tab endpoint. Run the **full** `vitest run` suite (cross-file regression lesson, #419/#420).

## Out of scope

- Balance reconciliation against the saldo columns (would be its own spec).
- OFX flow changes / format-selector UI redesign (extension auto-routing only).
- Matching the file's account number/currency to the chosen account (ignored; user picks account).
