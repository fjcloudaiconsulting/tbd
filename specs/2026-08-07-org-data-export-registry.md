# TBD-222 — Org data export: the registry and an operator CLI

Status: agreed, ready to build
Date: 2026-08-07

Two independent architects, then concede-or-defend. They crossed on three forks; those
are ruled in §9. **PR 1 ships NO HTTP endpoint and NO UI.**

---

## 1. The premise, corrected

The ticket says the export is *"promised on the landing page, which makes it a compliance
commitment."* **That is a misquote.** `frontend/components/landing/faqData.ts:16` says a
full org export *"is in the works"*.

The binding commitment is the **privacy policy**, `frontend/app/privacy/page.tsx:197-200`
(Portability) exercised via `:211-220` — email `privacy@`, **answered within 30 days**.
That is satisfiable today by an operator running a query.

**So this ticket closes an operational gap, not a compliance gap.** State that honestly:
the value is making the manual path repeatable and *provably complete* instead of an
ad-hoc dump that will omit tables exactly as `wipe_org_data` did.

⚠ **Shipping self-serve would RAISE the bar we are held to** — a subject would hold a
document they believe is complete. That asymmetry is why §4's guard is the real work and
why the endpoint is deferred behind a measured entry condition (§8).

## 2. Scope of PR 1

**Ships:** `app/services/export_registry.py` (49 hand-written dispositions),
`app/services/org_export_service.py` (NDJSON streaming), `backend/scripts/export_org.py`
(operator CLI), and the §4 fences.

**Does NOT ship:** any route, any React, any rate limit, any job queue, any object
storage. `services/scheduler/` is a fixed 7-job platform cron with no job-status table —
TBD-222 must not be the ticket that invents a durable job subsystem for one consumer.

**Why no endpoint (§9 fork 1, architect A conceded):** production is
`instance_count: 1` / `basic-xxs` (`.do/app.yaml:81-82`) with **no `--workers`**
(`backend/Dockerfile:59`) — one uvicorn process, ~512 MB, **no second instance to absorb
an OOM**, and `initial_delay_seconds: 30` on the health check. A buffered full-org
serialization inside a request is a ~30-45s total outage triggerable by one admin
clicking one button. Separately, an export endpoint collapses a household's entire
financial history into **one quiet authenticated request**, replacing an attack that
today costs ~50 rows per paginated call with noisy logs.

## 3. Format: NDJSON with a terminal trailer

⚠ **This resolves a genuine tension. Read both halves.**

Buffering risks OOM on a 512 MB single instance. Streaming risks a **truncated file
delivered as a success** — and for an artifact whose entire purpose is completeness, a
silently-incomplete document is worse than a slow one.

The property we need is **detectability**, not buffering:

```
line 1     {"record":"header","schema_version":1,"exported_at":…,"org_id":42,
            "org_name":…,"registry_version":…,"expected_tables":[…],
            "excluded":{"<table>":"<reason>",…}}
lines 2..N {"record":"row","table":"transactions","data":{…}}
last line  {"record":"trailer","tables":{"<table>":N,…},"total_rows":N,
            "sha256_body":"<hex of all preceding bytes>","complete":true}
```

**The trailer is the contract.** A file is complete iff (a) the last line parses as
`record=="trailer"`, (b) per-table counts match rows observed, (c) `sha256_body` matches.
The hash is computed **incrementally** (`hashlib.update()` per line as it ships) — integrity
without ever holding the document.

⚠ **A manifest-FIRST design was rejected**: a truncated file still parses and still looks
complete. The completeness record must be the thing truncation destroys.

CLI writes to `<name>.ndjson.part` and `os.rename`s only after the trailer flushes, so a
partial file never appears under the final name.

**No CSV.** The binding text says "machine-readable"; JSON satisfies it in one serializer.
CSV needs 30+ flattening specs and cannot represent `layout_json`, `params_json`,
`projection_json`, `detail`, `context`. The FAQ's CSV claim is **false today** and is filed
as **TBD-343**, a copy fix — false marketing copy must not dictate an architecture.

**Streaming discipline:** `select(table)` against `Base.metadata.tables[...]` returning
`Row` mappings, `.execution_options(yield_per=1000)`, keyset pagination on the PK. Never
build a list of all rows; never `json.dumps` the document.

**Bound it:** pre-flight `COUNT(*)` over included tables; above `EXPORT_MAX_ROWS` refuse
and point at the email channel. ⚠ **A row count does not bound bytes** —
`transactions.description` is free text and several columns are unbounded JSON. Also carry
`EXPORT_MAX_BYTES`, checked **incrementally against cumulative encoded output**, aborting
the moment it is crossed. Bytes is what kills the box, so bytes is what to count.

## 4. THE COMPLETENESS MECHANISM — the real work

`wipe_org_data` covers **15 of 49** tables under a docstring promising *"every new
org-scoped data table goes through this function"*. **A convention in a docstring is not a
mechanism.**

⚠⚠ **And the count itself drifted three times in this sprint's own briefs about drift**
(44 → 49 → "37 files" → 35). A hand-maintained list is not viable; the guard must be a
failing test.

### Leg 1 — runtime metadata vs a hand-written literal

```python
import app.models                      # populate metadata
from app.models.base import Base
from app.services.export_registry import EXPORT_DISPOSITION

runtime  = set(Base.metadata.tables)   # RUNTIME — SQLAlchemy declarative registry
declared = set(EXPORT_DISPOSITION)     # HAND-WRITTEN — a literal dict
assert not runtime - declared          # new table with no decision
assert not declared - runtime          # stale entry for a dropped/renamed table
```

Not tautological: `runtime` is produced by class creation (authored by whoever adds a
model, who is by hypothesis not thinking about the export); `declared` is typed by a human
who is. **Both directions required** — without the reverse assert, a renamed table leaves a
dead entry that makes the counts lie forever.

### ⚠ Leg 2 — filesystem vs runtime metadata (closes leg 1's blind spot)

**`Base.metadata` is populated ONLY by the imports in `app/models/__init__.py`.** A model
file not listed there is invisible to `Base.metadata` **and** to
`alembic/env.py`'s `target_metadata` — so **leg 1 stays GREEN while the table exists**.

```python
in_source = {m.group(1) for p in Path(app.models.__file__).parent.glob("*.py")
             for m in re.finditer(r'^\s*__tablename__\s*=\s*["\'](\w+)["\']',
                                  p.read_text(), re.M)}
assert in_source == set(Base.metadata.tables)
```

### Leg 3 — the registry must be the loop, not a document

A registry that decides but is not iterated is decoration, and "correct registry,
hand-maintained exporter" is this repo's signature half-fix.

```python
assert set(collect_export(db, org_id).tables) == {t for t,d in EXPORT_DISPOSITION.items() if d.included}
```

### Leg 4 — the scoping predicate

⚠ **`org_id` is NOT a sufficient predicate.** Six tables reach the org only by join —
`cc_cycle_payments`→`accounts`, `report_versions`→`reports`,
`transaction_tags`→`transactions`, and `notifications` /
`user_dismissed_announcements` / `user_notification_preferences`→`users`. Two more have
**nullable** `org_id` (`feedback_entries`, `rate_limit_overrides`), so `WHERE org_id = ?`
silently drops rows.

Seed org **B** with ≥1 row in **every** included table, export org **A**, assert **no**
B-sentinel appears anywhere in A's output.

### Named mutants — each RED, then GREEN on restore

| ID | Mutant | Kills |
|---|---|---|
| M-E1 | add `class _Probe(Base): __tablename__ = "_probe_tbl"` in an existing model file | leg 1, first assert |
| M-E2 | delete `"tags"` from the registry | leg 1, first assert |
| M-E3 | add `"gone_table": Exclude(...)` | leg 1, **second** assert |
| **M-E4** | create `models/_probe.py` with a `__tablename__`, **do not** import it in `__init__.py` | **leg 1 stays GREEN — that green is the finding.** leg 2 RED |
| M-E5 | registry says include, delete the table from the exporter's loop | leg 3 |
| M-E6 | `cc_cycle_payments` scope → `WHERE org_id` | leg 4 |
| M-E7 | `transaction_tags` → unfiltered `SELECT *` | leg 4 (the realistic bug — no `org_id` column, lazy fix is to skip the filter) |
| M-E8 | un-redact `users.password_hash` | §6 redaction fence |

⚠ **If M-E4 reports RED on leg 1, it was mis-run.** Leg 1 must stay green there.

## 5. Disposition rule

> **Include iff (provided by the subject) OR (readable by an org OWNER through the app).
> Exclude otherwise.**

A union, deliberately. Owner-readability alone collapses Art. 15 into Art. 20 and would
exclude `feedback_entries`, which the subject typed in. Provided-by alone would exclude
things an owner already reads.

### The four contested tables — ruled

- **`audit_events` → INCLUDE-REDACTED.** Scope `target_org_id = O` **only**.
  ⚠ **Never add an `actor_user_id` disjunct** — a superadmin's `admin.*` actions against
  *other* tenants carry their `actor_user_id` with a foreign `target_org_id`, so a
  disjunct exports other tenants' rows. Drop **`ip_address` and `detail` unconditionally**
  (a household partner's IP history is the Art. 20(4) harm; `detail` is free-form JSON
  and a field allowlist inside a blob is unauditable). Keep `actor_email` only where
  `actor_user_id IN (O.users)`.
- **`invitations` → INCLUDE, full.** The org **typed those addresses in**, and
  `MembersSection` already renders pending invites to admins. Exporting what the UI
  already shows creates zero new disclosure.
- **`ai_usage_ledger` → INCLUDE.** ⚠ Its reason string must be *"org-scoped, no
  third-party surface"* — **not** "behavioural data about identifiable users": the table
  has **no `user_id` column**, so there is no user attribution to be had. A wrong reason
  in the registry documents a falsehood.
- **`email_broadcast_recipients` → INCLUDE**, filtered to `user_id IN (O.users)`, **minus
  `error`** (a Mailgun provider diagnostic that leaks our infrastructure, not their data).
  ⚠ It has **no `org_id`** — an unfiltered read is a full-platform address book.

### Excluded (with reasons that ship in the header's `excluded` block)

Platform/global: `plans`, `system_settings`, `roles`, `role_permissions`, `announcements`,
`api_tokens` (no `org_id`; `token_hash` is a credential), `email_broadcasts`,
`merchant_dictionary`, `tag_dictionary`.
Operator config: `org_feature_overrides`, `rate_limit_overrides` (nullable `org_id`;
operator `note`), `org_data_reset_locks` (`lease_token` is a live capability).
⚠ **`tag_dictionary_contributors` — the one table where exporting would be an active
privacy breach.** Its own docstring says it is *never read by any API endpoint, never
serialized*; exporting `contributor_org_id` rows is precisely the de-anonymisation the
k-anonymity design prevents.

**Every remaining table is INCLUDE**, org-scoped or via the joins in §4 leg 4.

## 6. Redaction

Columns are **opt-out, not opt-in** — a new column on `transactions` lands in the export
automatically, which is right, because the drift we fear is *omission*.

**Never export:** `users.password_hash`, `totp_secret`, `recovery_codes`, `stepup_token`,
`stepup_token_expires_at`; `org_ai_credentials.encrypted_api_key`,
`encrypted_bearer_token`, `key_fingerprint`, and ⚠ **`base_url`** — a `String(512)` that
can carry `https://user:pass@host` in band, and which **matches no secret-name heuristic**.

⚠ **`password_changed_at` and `sessions_invalidated_at` ARE exported.** They are
datetimes, not credentials, and they are the subject's own security history — Art. 15
material. An earlier brief wrongly listed them as forbidden.

**Column-drift fence:** for every included table, any column whose name matches
`("password","secret","token","hash","encrypted","recovery","credential","cipher","salt",
"nonce","private_key","api_key","passphrase")` must be in that table's `redact` set or in
a `SECRET_NAME_ALLOWLIST` with a one-line justification. Two independent sources (a naming
heuristic vs per-table redact sets), neither derived from the other.
⚠ **`base_url` matches no pattern** — that is a known limit of a name heuristic, which is
why its redaction is stated explicitly here rather than left to the fence.

## 7. CLI, audit, and the wipe-parity fence

`backend/scripts/export_org.py` writes an `org.data.exported` audit row via
`record_audit_event` on the independent-session pattern, detail:
`{operator, org_id, org_name, row_counts_by_table, excluded_tables, byte_size, sha256, registry_version}`.
⚠ **`sha256` and `row_counts_by_table` are load-bearing** — they are what later proves the
artifact handed to a data subject matches what was generated. Row counts, never row
contents.

⚠ Per `reference_prod_db_readonly_access.md`, prod reads are operator-authorized and the
agent cannot `doctl compute ssh`. The CLI must be runnable as a DO console job writing to
stdout, with the operator redirecting to a file. Document that in `infra/`.

**Wipe-parity fence (drains into TBD-223).** Both `wipe_org_data` and
`delete_org_cascade` already return `dict[str, int]` keyed by table name — call each
against a fresh empty org in a test and read `set(counts)`. Assert every org-scoped
included table is either wiped or in an explicit `EXPORT_ONLY_ACKNOWLEDGED` set carrying a
per-entry TBD-223 note. This converts an invisible 28-table gap into an enumerated list.
⚠ `delete_org_cascade` emits `counts["settings"]` for the table `org_settings` (filed as
**TBD-354**) — alias it here rather than working around it silently.

## 8. Entry condition for the deferred endpoint

All four, no partial credit: (1) **measured peak RSS < 128 MB** from a CLI export of the
largest production org, taken from the streaming path, not estimated; (2)
`instance_count >= 2` or a documented operator decision to accept single-instance
exposure; (3) the registry has served one full TBD-223 cycle, proving it is not
export-shaped only; (4) re-auth, `2/hour`, `require_interactive_session`, audit and
admin-notification all implemented and fenced.

⚠ **Re-auth, not a typed confirmation phrase.** The reset uses a typed phrase because its
risk is *accident*; an export's risk is a *stolen session*, which a typed phrase does
nothing about. Do not "make it consistent" with the reset in review.

## 9. Where the ticket is wrong

1. **Premise is a misquote** (§1). Work is valid; the urgency argument is not.
2. **"A user's full data" names a thing that does not exist** — 33 tables key on `org_id`,
   none on a user partition. That contradiction is what produced the false owner-vs-member
   fork.
3. **Modelling completeness on `wipe_org_data` models it on the thing that failed** —
   15 of 49, under a docstring promising the opposite.
4. **effort-m is right only for this scope.** As the ticket implies (registry + streaming
   + endpoint + UI + async) it is L. Keep the label; subtract the surface.
5. **The "EU-hosted" note is not a requirement** and should be struck — it constrains
   delivery, not content.
6. ⚠ **TBD-223's own guidance is a trap** — "build on `wipe_org_data`" would ship an
   erasure leaving 28 tables of personal data intact. A comment already warns it.
7. **Every surface says "organization", never "your data"** — a shared household ledger is
   joint personal data (Art. 20(4)). One architect caught themselves writing a card titled
   "Export your data" above body copy saying "your organization". Constraint is already
   pushed into TBD-343's DoD.
