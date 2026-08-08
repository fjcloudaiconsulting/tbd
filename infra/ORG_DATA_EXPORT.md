# Org data export — operator runbook (TBD-222)

Answers a GDPR Art. 15 / Art. 20 request for one organization's data.

## What this is, and what it is not

The binding commitment is the **privacy policy** (`frontend/app/privacy/page.tsx`,
Portability): a request to `privacy@` is answered **within 30 days**. It is
satisfiable by an operator running the command below.

There is **no self-serve endpoint and no UI**, deliberately. Shipping one would
raise the bar we are held to — a data subject would hold a document they believe
is complete — and a buffered full-org serialization inside a request on the
current `instance_count: 1` / `basic-xxs` deployment is a multi-second outage
triggerable by one click. The entry condition for revisiting that is in
`specs/2026-08-07-org-data-export-registry.md` §8.

So this tool exists to make the manual path **repeatable and provably complete**,
not to close a compliance gap.

## Running it

The classifier blocks an agent's `doctl compute ssh`; an operator runs these.

### On the DO App Platform console

The CLI writes the artifact to stdout and every diagnostic to stderr, so a plain
redirect captures only the export:

```bash
python -m scripts.export_org --org-id 42 --stdout \
  --operator you@yourdomain.tld > org-42.ndjson
```

### Locally, or anywhere with a writable disk

```bash
python -m scripts.export_org --org-id 42 --out /tmp/org-42.ndjson \
  --operator you@yourdomain.tld
```

File mode writes `/tmp/org-42.ndjson.part` first and renames it into place only
after the trailer flushes, so a killed run never leaves a partial file under the
final name.

### Sizing it first

```bash
python -m scripts.export_org --org-id 42 --dry-run
```

Prints per-table `COUNT(*)` without producing anything. Do this before exporting
an unfamiliar org.

## Verifying the artifact before you send it

**Always do this.** An NDJSON export is complete iff its **last line** is a
trailer whose per-table counts match the rows present and whose `sha256_body`
matches every preceding byte.

```python
from app.services.org_export_service import verify_export

with open("org-42.ndjson", "rb") as fh:
    result = verify_export(fh)
print(result.ok, result.reason, result.total_rows)
```

A truncated file is well-formed NDJSON all the way down and its header still
claims the full table list — the **only** thing that betrays it is the missing
trailer. That is why the trailer is last, and why a manifest-first layout was
rejected. Do not skip verification because the file "looks right".

## Refusals

| Exit code | Meaning |
|---|---|
| `0` | complete artifact produced |
| `2` | no such org |
| `3` | over `EXPORT_MAX_ROWS` or `EXPORT_MAX_BYTES` — use the `privacy@` channel and a narrower request |
| `4` | stream ended with no trailer; nothing was renamed into place |

## Audit trail

Every successful run writes an `org.data.exported` row to `audit_events`,
visible at `/admin/audit`, carrying `sha256`, `row_counts_by_table`,
`byte_size`, `excluded_tables` and `registry_version`. The `sha256` is what
later proves the artifact you handed over is the one that was generated —
**record which file you sent**. Row counts only; no row contents.

## What is and is not in the export

`app/services/export_registry.py` holds one hand-written disposition per table,
and `backend/tests/services/test_export_registry.py` fails CI if a new model
lands without one. The export's own header carries the `excluded` block with a
reason per withheld table, so the artifact is self-describing.

Withheld categories: platform-global tables (plans, roles, announcements,
system settings), operator configuration (feature overrides, rate-limit
overrides, reset locks), and `tag_dictionary_contributors` — exporting that one
would de-anonymise other orgs' contributions, which is the opposite of a privacy
measure.

Redacted columns: password hashes, TOTP secrets, recovery codes, step-up tokens,
encrypted AI credentials and their `base_url`, `audit_events.ip_address` and
`detail`, and the Mailgun `error` diagnostic. Note that `password_changed_at`
and `sessions_invalidated_at` **are** exported — they are datetimes and they are
the subject's own security history.

## Erasure is a different question

An org-scoped table being exported does **not** mean erasure reaches it.
`backend/tests/services/test_export_wipe_parity.py` enumerates the tables that
are exported but not wiped — 17 at time of writing — each carrying a TBD-223
note. That list is the erasure backlog; do not read it as a list of exemptions.
