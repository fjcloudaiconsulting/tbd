# Platform Permissions Decisions

Date: 2026-04-24
Status: Accepted
Scope: L4.1 permission groundwork for platform-level authorization

## Context

The product already has org-scoped roles on `User.role` (`owner`, `admin`, `member`) and a separate platform-level `is_superadmin` flag.

L4.1 introduces permission checks for platform-only capabilities such as plan management. Before implementation, we needed to lock down:

- where the role-to-permission mapping lives
- how permissions are named and type-checked
- how platform roles are resolved from the current user model
- what is explicitly out of scope for this phase

## Decisions

### 1. Role-to-permission mapping lives in code

We will use an in-code Python mapping, not DB tables and not YAML config.

Planned location:

- `backend/app/auth/permissions.py`

Shape:

```python
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {}
```

Why:

- fastest path for L4.1
- no DB schema or seed migration required
- easy to review in Git
- current scope is only one platform role path: `is_superadmin`

Rejected for now:

- DB tables (`roles`, `permissions`, `role_permissions`): deferred until L4.8 role admin UI
- YAML config: adds indirection without solving runtime editability

### 2. Permissions are defined as a typed `Literal`

Permissions will be declared in one place with a `Literal[...]` alias.

Planned shape:

```python
Permission = Literal[
    "plans.manage",
]
```

Why:

- catches permission-name typos at call sites
- IDE autocomplete is better than raw string usage
- keeps the permission namespace explicit
- simple to extend one permission at a time

Recommended companion constant:

```python
ALL_PERMISSIONS: frozenset[Permission] = frozenset({
    "plans.manage",
})
```

This gives one canonical set when iterating or seeding later.

### 3. Platform role resolution is separated from permission mapping

Permission evaluation should not read `user.is_superadmin` directly everywhere.

Planned helper:

```python
def _platform_roles(user: User) -> frozenset[str]:
    roles: set[str] = set()
    if user.is_superadmin:
        roles.add("superadmin")
    return frozenset(roles)
```

Why:

- separates assignment source from authorization logic
- keeps future L4.8 role-assignment changes localized
- callers continue asking only for permissions

### 4. `is_superadmin` remains a hard bypass

`superadmin` is treated as "grants all permissions" via `has_permission(...)`, not by exhaustively listing every permission in `ROLE_PERMISSIONS`.

Why:

- new permissions automatically apply to superadmins
- avoids drift when a new permission is added but the map is not updated
- matches the current product expectation for superadmin access

Important note:

- `_platform_roles()` is still useful as the future seam for role assignment
- `ROLE_PERMISSIONS` is therefore not the exhaustive source of truth while `is_superadmin` exists as a dedicated bypass

### 5. Existing org roles are out of scope

The existing `User.role` enum (`owner`, `admin`, `member`) remains org-scoped and is not part of L4.1 platform permissions.

Why:

- different concern and scope
- avoids mixing org authorization with platform authorization
- keeps L4.1 focused on the platform-only superadmin path

### 6. No new DB field or join table in L4.1

We are not adding a platform-role column or a role-assignment join table in this phase.

Why:

- current source of truth is already `User.is_superadmin`
- avoids schema cost before there is a UI or operational need for configurable roles

### 7. Guard API — two names, dependency-first

The module exports exactly two callables. No third idiom.

```python
def has_permission(user: User, permission: Permission) -> bool: ...

def require_permission(
    permission: Permission,
) -> Callable[..., Awaitable[User]]: ...
```

- `has_permission` is the pure predicate, used for in-handler conditional branching only (e.g. shaping a response field based on permission).
- `require_permission` is a FastAPI dependency factory. It composes with `get_current_user` so 401 (missing / invalid token) stays upstream and 403 (authenticated but insufficient) is the only code path that emerges from the permission check itself.
- The inner dependency returns `User` on success, so call sites that genuinely need the user can inject it directly and avoid double-declaring `get_current_user`.
- Evaluation order inside `has_permission` is fixed: `is_superadmin` short-circuits first, then `_platform_roles` + `ROLE_PERMISSIONS`. Unknown roles contribute no permissions; unknown permission strings (passed dynamically) deny by default.
- The inner dependency's `__name__` is overridden to `require_permission_<permission_with_dots_as_underscores>` so FastAPI's dependency tree stays readable under introspection.

### 8. Route-level dependencies are the default; signature injection only when the handler reads `User`

When a handler does not reference the authenticated user in its body, the gate is applied at the decorator:

```python
@router.post(
    "",
    response_model=PlanResponse,
    status_code=201,
    dependencies=[Depends(require_permission("plans.manage"))],
)
async def create_plan(
    body: PlanCreate,
    db: AsyncSession = Depends(get_db),
):
    ...
```

When the handler actually uses the user (for org scoping, audit logging, conditional response shaping), signature injection returns the authenticated user and avoids a second `Depends(get_current_user)`:

```python
async def some_handler(
    current_user: User = Depends(require_permission("orgs.impersonate")),
    ...
): ...
```

Why:

- honest handler signatures — no unused `current_user` parameter on the majority of gated routes
- same 401/403 semantics either way
- avoids training future edits to reach for the heavier pattern by default

### 9. Response-body text change is intentional

Prior behaviour: `_require_superadmin` raised `HTTPException(403, detail="Superadmin access required")`. The new `require_permission(...)` raises `HTTPException(403, detail="Forbidden")`.

Status code is unchanged (403). The detail string changes.

This is a user-visible difference: `frontend/lib/api.ts::apiFetch` lifts `detail` into `ApiResponseError.message` and `extractErrorMessage` surfaces that string. A client that previously displayed `"Superadmin access required"` for a 403 will now display `"Forbidden"`.

Accepted intentionally:

- uniform 403 body across all permission gates (L4.2 – L4.10) with no permission-name leakage
- handler identity available in access logs for server-side debugging
- existing frontend error paths render the new string without additional wiring
- the prior string was specific to the old helper; the new string is consistent with the generic guard

If a future product requirement wants a user-visible reason on 403, we'll add it at that point — not pre-emptively.

## Consequences

### Benefits

- low-risk implementation
- fast path to permission-gated platform features
- clear migration path to DB-backed RBAC later

### Trade-off accepted

If a configurable non-superadmin platform role is needed before L4.8, we will either:

- deploy a code change to update the in-code map, or
- bring the DB-backed role work forward

This trade-off is acceptable pre-launch.

## Future Migration Path

When L4.8 introduces role administration:

1. Keep permission names stable.
2. Create DB-backed role/permission tables.
3. Seed the initial DB rows from `ALL_PERMISSIONS` and `ROLE_PERMISSIONS`.
4. Move `has_permission(...)` to read from DB-backed assignments.
5. Remove or reduce the special-case bypass only if product requirements change.

## Scope of the L4.1 PR

### Files added

- `backend/app/auth/__init__.py` (empty — makes the package explicit)
- `backend/app/auth/permissions.py` (the module described in decisions 1–7)

### Files modified

- `backend/app/routers/plans.py`:
  - remove the `_require_superadmin` helper
  - apply `dependencies=[Depends(require_permission("plans.manage"))]` to `list_all_plans`, `get_plan`, `create_plan`, `update_plan`, `delete_plan`
  - drop the `current_user` parameter from those five handlers (they never read it)
  - `list_plans` is intentionally not superadmin-gated and is not touched
  - leave the `User` / `get_current_user` imports in place because `list_plans` still uses them

No other production file changes. No Alembic migration. No frontend change.

### Explicit out of scope for L4.1

These are intentionally not addressed in this PR; they're recorded here so later readers see deliberate deferral rather than oversight.

1. Hybrid gates in `routers/subscriptions.py` and `routers/settings.py` — org-role-based, addressed in a later authz phase.
2. The org-level `Role` enum (`OWNER` / `ADMIN` / `MEMBER`) and its inline comparisons across the codebase — stays as-is.
3. DB-backed `roles` / `permissions` / `role_permissions` tables — scheduled for L4.8.
4. `UserResponse.permissions: list[str]` field for the frontend — added when the first real granular frontend gate appears (likely L4.2 or L4.5).
5. Denial / authz-failure audit logging — L4.7.
6. `has_any_permission` / `has_all_permissions` helpers — deferred until a real call site needs them.
7. Alternative platform-role assignment paths beyond `is_superadmin=True` — L4.8.

## Verification

No automated test harness exists anywhere in the project today. Verification for L4.1 is:

1. **Syntax + reload.** `python3 -m py_compile backend/app/auth/permissions.py backend/app/routers/plans.py` post-edit. Backend is running under watch-reload; `./pfv logs backend` should show `Application startup complete` with no import errors.
2. **Grep sweeps** (all should return the noted results):
   - `grep -rn "_require_superadmin" backend/app` → no matches.
   - `grep -n "get_current_user" backend/app/routers/plans.py` → only on `list_plans`'s signature.
   - `grep -n "User" backend/app/routers/plans.py` → still references `User` only if `list_plans` keeps `current_user: User = Depends(get_current_user)`; if even that goes, the import goes too.
3. **Manual smoke against the five gated handlers** using the seeded superadmin plus a second non-superadmin user:

    | Endpoint | superadmin | non-superadmin | anonymous |
    |---|---|---|---|
    | `GET /api/v1/plans` | 200 | 200 (intentionally open) | 401 |
    | `GET /api/v1/plans/all` | 200 | 403 | 401 |
    | `GET /api/v1/plans/{id}` | 200 | 403 | 401 |
    | `POST /api/v1/plans` | 201 | 403 | 401 |
    | `PUT /api/v1/plans/{id}` | 200 | 403 | 401 |
    | `DELETE /api/v1/plans/{id}` | 204 | 403 | 401 |

4. **401 vs 403 spot-check.** The same endpoint with no token returns 401 (from `get_current_user` upstream); with a valid non-superadmin token returns 403 (from `require_permission`). Distinction preserved end-to-end.

## Rollout

- Single PR. No Alembic migration. No DO deploy gating.
- One commit acceptable (or split add-module / convert-callsites if review prefers a narrower diff).
- Reversibility: one-file revert restores `_require_superadmin` and the five handler signatures. No DB state to unwind.
- Merge order: L4.1 must land before L4.2 — the admin dashboard route will be the first consumer of the new gate shape.

## Migration pattern for future L4.x PRs

Each subsequent PR in the L4.x series that introduces a new permission follows the same shape, documented here once so the module docstring can stay terse:

1. Add the permission name to the `Permission` `Literal` and to `ALL_PERMISSIONS`.
2. If a non-superadmin role should grant it, add the entry to `ROLE_PERMISSIONS`. Empty for L4.2 – L4.7; only L4.8 introduces non-superadmin roles.
3. Gate the route at the decorator via `dependencies=[Depends(require_permission("resource.action"))]`, or inject into the signature if the handler actually reads the user.
4. If the frontend needs permission-aware UI, add the field to `UserResponse.permissions` (wire-up lands when the first real case appears).

The `backend/app/auth/permissions.py` module docstring points back to this decision doc rather than restating the pattern, so there is one source of truth.
