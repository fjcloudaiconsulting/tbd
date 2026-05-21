---
name: ci-test-sharding-followup
description: "Architect-aligned plan to shard backend pytest in GitHub Actions after a timing audit; target <3 min backend check, preserve full-suite semantics"
---

# CI test sharding — near-term follow-up (2026-05-17)

## Trigger

Backend pytest runtime has grown materially during the parallel-team wave:

- After PR #305 (Team I PR 1): ~270s
- After PR #306 (Team I PR 2): ~275s
- After PR #307 (Team I PR 3): ~308s
- After PR #308 (Team I PR 4 + architect patches): ~298s
- After PR #309 (cold-start UX) + others: backend remains in the 4:30–5:00 minute band.

GitHub Actions backend check is the slowest gate on every PR. The wave shipped fine, but the trend is non-trivial as we add more session-model concurrency tests, integration coverage, etc.

## Architect-aligned approach (do NOT skip the audit)

1. **Measure first** — timing audit per test file. `pytest --durations=50` against current `main` to see where the runtime is actually concentrated. Likely candidates: integration tests, Redis-heavy auth tests, full-suite migration boots.
2. **Target backend pytest only** — it is the obvious first target. Frontend Vitest is currently ~11s for 1017 tests; not material.
3. **Start with 4 shards, not 10** — the article cited went to 10; architect recommends 4 first. Less efficiency loss per shard, easier to debug contention.
4. **Deterministic ordering** — either `pytest-split` (uses a recorded duration file checked into the repo) or `pytest-xdist` with `--dist=loadgroup` to keep related tests together. NO random ordering unless tests are proven fully isolated.
5. **Preserve full-suite semantics** — fixtures, autouse, conftest, allowlist regressions etc. must still see the same view of the codebase. Allowlist tests like `test_sessions_invalidated_at_allowlist.py` scan files; they must run in exactly one shard or be replicated to all (one shard is cheaper).
6. **DB / container contention is the hidden trap** — each shard needs its own MySQL + Redis. Either spin a stack per shard in GH Actions, or move all integration tests into a single shard while sharding pure-unit tests across the others.

## Target

- **Backend check wall-clock < 3 minutes.**
- No flaky tests masked by parallelism (would manifest as intermittent failures that pass on rerun — keep an eye out during the first few weeks).
- No new CI minutes blow-up — running 4 shards in parallel costs ~4× the minutes for the same wall-clock, so the total minute cost is similar (within 10-20% overhead).

## Implementation order (when picked up)

1. Run `pytest --durations=50` on current main → produce a ranked list. Save the report somewhere referenceable.
2. Decide between `pytest-split` (deterministic, recorded times) vs `pytest-xdist` (work-stealing). Start with `pytest-split` — it has better debug ergonomics.
3. Add a new GH Actions matrix step. 4 shards. Each shard spins its own MySQL + Redis via `docker compose -p team-shard-N` (same pattern as the agent-isolation rule already in CLAUDE.md).
4. Replicate the allowlist-scan test to all shards OR pin it to shard 0. Either works; replication is simpler.
5. After two weeks of clean runs, optionally bump to 6 or 8 shards if the wall-clock is still over 3 minutes.

## Out of scope

- Frontend Vitest sharding (not material at current runtime).
- Test parallelism within a single pytest process via `pytest-xdist -n auto` (different concept; can stack on top of GH-Actions sharding later if useful).
- Rewriting tests to be faster (would help but is a much bigger initiative).

## Relates to

[[project_status]] — wave 2026-05-17 is complete; this is the natural next CI improvement.
