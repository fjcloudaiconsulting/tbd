# SUPERADMIN_RECOVERY.md

Audience: the operator on call when the platform has lost its last active superadmin, or when a second one has to be added. Ticket: TBD-443.

## 1. Why this exists

`is_superadmin` gates every `/admin` route. Nothing in the request path ever grants it after an account is created: the bootstrap grant fires only on an empty `users` table (TBD-365), and an org invitation always writes `False`. So an install with zero **active** superadmins cannot be administered from inside the app: `/admin` returns 403 to everyone and there is no promotion endpoint. Before TBD-443 the only fix was hand-written SQL against production.

The fix is an operator CLI that runs inside the backend container. Being able to exec into that container is the proof of identity: the container already holds the database credentials. The CLI adds no privilege. It adds refusals, a typed confirmation and an audit row. An in-app promote/demote endpoint was rejected on purpose: a stolen superadmin session could use it to mint a persistent backdoor account, or demote the real superadmin.

## 2. Detection

| Signal | Where | Meaning |
|---|---|---|
| `platform.superadmin.none` (ERROR log) | scheduler tick, `superadmin_floor` job | Zero active superadmins on a populated install. **Page.** |
| `platform.superadmin.single` (WARNING log) | same job, every tick | Exactly one. A single point of failure, not an outage. Low priority. |
| `tbd.platform.active_superadmins` (gauge) | same job | The count. Alert at 0; also alert when the series goes stale (the ticker stopped). |
| Amber notice on `/admin` | admin dashboard | Exactly one. The dashboard can never show zero: nobody can open it then. |

The count is `is_superadmin AND is_active`: a deactivated superadmin cannot sign in, so it does not count. An install with no users at all stays quiet (that is a fresh install).

## 3. Preconditions on the target account

The CLI refuses, writes nothing to the user row, and records a `failure` audit row unless the target:

1. is active (not deactivated, soft-deleted or merged; reactivating a deactivated superadmin is TBD-377, not this tool);
2. has a verified email;
3. has MFA enrolled (TOTP, self-serve from the account's security settings, including for Google sign-in accounts);
4. is the first active superadmin, **or** the operator passes `--additional`.

A target that already is an active superadmin is a no-op: exit 0, nothing written.

Have the person sign up (or use their existing account), verify their email and enrol MFA **before** you run the CLI.

## 4. Running it

Exec into the running backend pod (namespace and deployment per the infra private ops notes):

```
kubectl -n <namespace> exec -it deploy/backend -- \
  python -m scripts.promote_superadmin \
    --email <their sign-in address> \
    --operator <your name> \
    --reason "<incident or ticket id>"
```

Locally: `docker compose -p <project> exec backend python -m scripts.promote_superadmin ...`.

- `--email` or `--user-id` (exactly one) selects the target.
- `--operator` (required) is recorded as the audit row's actor.
- `--reason` (required, at most 200 characters) is recorded in the audit row.
- `--additional` grants even though an active superadmin already exists. Use it to get from one to two.
- `--yes` skips the typed confirmation. Required when there is no terminal (a one-shot job); otherwise the CLI prints the target's `user_id`, `org_id` and the current active-superadmin count, and asks you to type the `user_id` back.

The CLI prints ids and refusal codes only, never the email you typed.

| Exit | Meaning |
|---|---|
| 0 | Promoted, or already an active superadmin |
| 2 | Bad arguments |
| 3 | No such user |
| 4 | Refused; the code is printed (`target_inactive`, `target_email_unverified`, `target_mfa_not_enabled`, `active_superadmin_exists`) |
| 5 | Not confirmed (wrong id typed, or no terminal and no `--yes`) |

## 5. What the grant does

In one transaction, under a row lock on the target:

- sets `is_superadmin = True`;
- sets `sessions_invalidated_at`, so every session the target already had (a stolen one included) is ended and the person signs in again before acting as superadmin;
- writes an `admin.superadmin.promoted` audit row with `outcome="success"`, `actor_email=<--operator>`, `target_org_id` of the target, and `detail` holding `target_user_id`, `via="operator_cli"`, `additional`, `prior_active_superadmin_count` and `reason` (no email).

## 6. Verify

1. The person signs in and opens `/admin`.
2. `/admin/audit` shows the `admin.superadmin.promoted` row.
3. On the next scheduler tick the gauge moves and the `platform.superadmin.none` / `.single` log stops (if the count reached two).

## 7. After a recovery

Get to **two** active superadmins as soon as possible (`--additional`), so the next lost account is not an outage. There is no demote tool: removing the flag is still a direct database change, deliberately, because a demote path is what would make zero reachable.
