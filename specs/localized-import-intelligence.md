---
name: Localized Import Intelligence (long-term thread)
description: Architect direction (2026-05-02) for evolving import categorization beyond the global deterministic L3.10 baseline into a locale-aware system. Captures the lookup order, telemetry shape, and per-locale extensions planned post-L3.10.
type: project
---
## Direction (architect-locked 2026-05-02)

PFV's import pipeline must eventually handle merchants, dates, numbers, and bank descriptors **per country/locale**, not just globally. L3.10 ships deterministic and global; this thread evolves it. The current `merchant_dictionary` is global token → category slug, which works for English/Iberian merchants but misses entirely on real Dutch banking data (verified 2026-05-02 against 345-row ING NL CSV: 0/345 coverage with the original Iberian/UK seed).

**Lookup order (locked):** `org_rules → locale-aware dictionary → AI fallback → default`. AI is a resolver for ambiguous localized descriptors, NOT the first layer.

## What's true today (post-L3.10)

- **`merchant_dictionary` schema:** global, no locale columns. Single normalized_token column.
- **Seed scope:** Iberian + UK + a few Dutch additions (KPN, ODIDO, THUISBEZORGD, JUMBO, AYVENS, FRANK ENERGIE) added in L3.10's Path B fix-pass. Coverage on the user's real ING data lifts from 0% → ~3.5% with these additions; further lift requires locale-aware normalization (city/country tail stripping) that's been deferred.
- **Normalization** (`category_rules_service.normalize_description`): global. Strips bank-noise prefixes, masked card prefixes, IBAN tails, terminal IDs, dates, NFKD-folds accents, strips trailing company-form suffixes (B.V., N.V., S.A., BVBA, GMBH, AG, INC, LLC, LTD).
- **Metrics scaffold** (`smart_rules.preview_built` and `smart_rules.import_executed` + `smart_rules.miss`): captures `org_id`, `normalized_token`, source-split counts. **Forward-compatible** — structlog kwargs are additive; future locale tags slot in without breaking existing log consumers.
- **CSV parser:** assumes ISO-ish dates and US decimal. Doesn't auto-detect `YYYYMMDD`, comma-decimal, or semicolon delimiter (real ING NL format).
- **Account model:** no `country_code`, no `currency`, no `source_bank`. All scoping is by `org_id` only.

## Future work (sequenced — see `project_roadmap.md` P-IL section)

1. **P-IL.1 — Locale hints on accounts + CSV imports.** Country, currency, source-bank fields where derivable. CSV uploader detects format hints (semicolon + comma-decimal + YYYYMMDD ≈ NL/DE export). User can override per import.

2. **P-IL.2 — Per-locale `merchant_dictionary` extension.** Add optional `country_code` and `source_bank_hint` columns; lookup falls back to global match if no locale-specific entry. Migrate the Dutch additions from L3.10 into NL-locale rows.

3. **P-IL.3 — Locale-aware normalization.** Country-specific tail-stripping. The single biggest unlock for Dutch coverage today: strip trailing city/country tokens like `AMERSFOORT NLD`, `LISBOA`, `DUBLIN IRL`. Out of scope for L3.10 (architect approved). Likely a `_TRAILING_LOCATION_TOKENS` set keyed by `country_code`.

4. **P-IL.4 — Miss-token telemetry with locale tags.** Extend `smart_rules.miss` to include `country_code`, `currency`, `source_bank` when known. Lets us measure dictionary gaps per market. **Privacy:** never log raw descriptions, only `normalized_token` + locale metadata.

5. **P-IL.5 — LAI.1 prompt locale-awareness.** When the LLM fallback ships, pass locale as system context so the model resolves "JUMBO ... NLD" without seeing PII.

6. **P-IL.6 — Date/number CSV parser per locale.** Required for L3.2 import hardening on non-EN/US banks. Currently a hard miss for ING NL (semicolon, comma-decimal, YYYYMMDD).

## Privacy invariants (carry forward)

- `org_id` and `normalized_token` are loggable. Raw transaction descriptions are NOT.
- Cross-org `vote_count` bumps in `merchant_dictionary` only happen when the org has explicitly opted in via `org_settings.share_merchant_data`.
- Per-locale dictionary extensions follow the same rule: no raw text crosses an org boundary; only canonical normalized tokens + locale metadata.
- LLM fallback (LAI.1) sends only the description string + locale hint, never amount/account/org context. Already documented in the LAI tier section of the roadmap.

## Why the current architecture is forward-compatible

- The lookup function `infer_category(db, *, org_id, description)` already returns `(cat_id, source)` — the source dimension extends to `"locale_dictionary"` later without API change.
- `merchant_dictionary` is a single table — adding nullable `country_code`/`source_bank_hint` columns is a non-breaking migration. Existing rows become "global" entries (NULL = match-anything).
- Metric events take arbitrary kwargs — adding `country_code=...` is non-breaking.
- The trailing-company-form strip in normalization is opt-in via `_TRAILING_COMPANY_FORMS` constant — locale-specific stops can be added per country without rewriting the function.

## How to act on this thread

When ANY of these signals fire, revisit this thread:
- Real users in a new market report low suggestion coverage.
- `smart_rules.miss` aggregate logs show a dominant non-English token cluster.
- A new bank format is added that the parser doesn't handle.
- LAI.1 implementation begins (P-IL.5 dependency lights up).

Don't preemptively build P-IL.1 through P-IL.6. The architect's framing: ship L3.10, measure, iterate.
