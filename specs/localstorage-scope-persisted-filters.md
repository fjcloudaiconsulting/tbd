---
name: localStorage Scope for Persisted Sort/Filter Keys (post-#195)
description: PR #195 introduced sort + filter persistence keys keyed by surface only (`pfv:sort:transactions`, etc). Reviewer flagged as non-blocking residual: scope by org/user when shared-browser org switching becomes common.
type: project
originSessionId: 0bf77f16-13ef-4926-8a64-7a5ddd96efc6
---
**Captured 2026-05-10.** Non-blocking residual flagged by external reviewer when approving PR #195. Park for post-launch.

## Current state (post-#195)

`frontend/lib/hooks/persisted-keys.ts` defines flat keys:
- `pfv:sort:transactions`
- `pfv:sort:accounts`
- `pfv:sort:dashboard-spending`
- `pfv:sort:dashboard-transactions`
- `pfv:filters:transactions`

These are NOT scoped by user or org. Two consequences worth noting:

1. **Same-browser, multi-org user.** If a user belongs to multiple orgs and switches between them, they see the same persisted sort and filter state across orgs. That can be a feature (consistent UX) or a bug (filters that made sense for org A don't for org B). Today it's not common; post-launch with multi-org users it might bite.

2. **Same-browser, multiple-user accounts.** A shared device (e.g., couple sharing a laptop) where partner A and partner B each have their own account: today, both inherit each other's persisted sort/filter state. Harmless for sort direction, potentially confusing for filters.

## Suggested fix shape

When this becomes worth doing, scope keys via the current user/org context:
- `pfv:sort:transactions:org-${orgId}` or `pfv:sort:transactions:user-${userId}`
- Decide which scope: probably `org-${orgId}:user-${userId}` for full isolation; or just `user-${userId}` if filter context is per-user not per-org
- Keys become a function rather than a constant: `sortKey(orgId, userId, "transactions")`
- The `usePersistedSort` and `usePersistedFilters` hook signatures take the dynamic key
- On user logout / org switch, the keys swap automatically because the hook reads the current context

## Why park

- Pre-launch: friends-testing only, low likelihood of multi-org or shared-browser usage matching the failure mode
- Single-user solo accounts (the seed user) experience zero impact
- Easy to retrofit when/if needed; no migration of existing localStorage values required (old keys just become orphaned)

## Cross-references

- PR #195 (`feat/sort-filters-persistence`) — the work that introduced the unscoped keys
- `frontend/lib/hooks/persisted-keys.ts` — the file to change when scoping is added
- `frontend/lib/hooks/use-persisted-sort.ts`, `use-persisted-filters.ts` — the hooks that consume the keys
