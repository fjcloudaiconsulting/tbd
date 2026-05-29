# SEO admin config UI — backlog

**Created:** 2026-05-29 (split-off from the baseline-SEO spec because the operator wants this in the future but not in the baseline PR).
**Parent spec:** `specs/2026-05-29-hide-payments-seo-baseline-ollama-lan.md` §2.

## Intent

Today the per-route `title` / `description` / OG / `robots` is baked into each `page.tsx` via Next.js's `export const metadata`. The operator wants to tune SEO without a code deploy — for example, changing the landing title for an A/B test, refreshing keyword-rich descriptions per route, or pointing a route at a different OG image.

## Sketched data model

New table `seo_overrides`:

| col | type | notes |
|---|---|---|
| `id` | int PK | |
| `route` | varchar(255) UNIQUE | `/`, `/login`, etc. Match Next.js's pathname. |
| `title` | varchar(255) NULL | overrides metadata.title.absolute |
| `description` | varchar(255) NULL | |
| `og_image_url` | varchar(512) NULL | absolute URL or path under `/og/` |
| `robots_index` | tinyint NULL | NULL = inherit; 0/1 = override |
| `keywords` | text NULL | comma-separated, for ops reference only (no SERP impact) |
| `created_by_user_id`, `updated_by_user_id`, `created_at`, `updated_at` | audit columns | |

## Plumbing

- Wrap `generateMetadata` on each public route to merge with `SeoOverride.find_by_route(pathname)`. Override values win; nulls fall through to the hardcoded metadata.
- Cache lookups for ~60 seconds (Next.js `revalidate`) to avoid hitting DB per request.
- Admin UI under `/system/seo` (superadmin-only): table of all routes, edit modal, OG image preview, "publish" button (just writes the row; revalidation kicks in on next render).

## Out of scope when picked up

- A/B testing infrastructure (drives operator policy, not data model).
- Per-locale overrides (single-language site for now).
- `hreflang` automation.
- Indexability scheduling / TTLs (just edit-now-or-later).

## Why it's not v1

- Adds DB write surface + cache invalidation + audit logging — non-trivial for an operator who hasn't validated demand for routine SEO edits.
- Per-route metadata in `page.tsx` is fine for the first 90 days of marketing — content changes trigger code deploys anyway.
