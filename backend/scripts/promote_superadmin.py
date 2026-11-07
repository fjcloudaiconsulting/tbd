"""Operator CLI: grant the platform superadmin flag (TBD-443).

Usage from inside the backend container::

    python -m scripts.promote_superadmin --email <address> \\
        --operator <your name> --reason "<ticket or incident>"
    python -m scripts.promote_superadmin --user-id 42 --additional \\
        --operator <your name> --reason "<why a second one>" --yes

Running it requires a shell in the backend container, which already holds the
database credentials: that is the operator's proof of identity. The write and
every refusal live in ``app/services/superadmin_recovery_service.py``; this
file only resolves the target, confirms it with the operator and maps the
outcome to an exit code. Runbook: ``docs/operations/SUPERADMIN_RECOVERY.md``.

Output names ids and refusal codes, never the email typed on the command line.

Exit codes: 0 promoted or already a superadmin, 2 usage, 3 no such user,
4 refused (code printed), 5 the operator did not confirm.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from app.database import async_session, engine
from app.services.superadmin_recovery_service import (
    REASON_MAX_LENGTH,
    PromotionRefused,
    count_active_superadmins,
    find_target,
    promote_superadmin,
    refusal_code,
)

EXIT_OK, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_NOT_CONFIRMED = 0, 2, 3, 4, 5


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _run(args: argparse.Namespace, *, confirm=input) -> int:
    async with async_session() as db:
        target = await find_target(db, email=args.email, user_id=args.user_id)
        if target is None:
            _log("error: no such user")
            return EXIT_NOT_FOUND
        user_id, org_id = target.id, target.org_id
        active = await count_active_superadmins(db)
        code = refusal_code(target, active_superadmins=active, additional=args.additional)
        already = target.is_superadmin and target.is_active

    _log(f"target user_id={user_id} org_id={org_id}; active superadmins now: {active}")
    if not already and code is None and not args.yes:
        if not sys.stdin.isatty():
            _log("error: no terminal to confirm on; pass --yes")
            return EXIT_NOT_CONFIRMED
        if confirm(f"type the user id ({user_id}) to confirm: ").strip() != str(user_id):
            _log("not confirmed; nothing written")
            return EXIT_NOT_CONFIRMED

    try:
        result = await promote_superadmin(
            async_session,
            user_id=user_id,
            operator=args.operator,
            reason=args.reason,
            additional=args.additional,
        )
    except LookupError:
        _log("error: no such user")
        return EXIT_NOT_FOUND
    except PromotionRefused as exc:
        _log(f"refused: {exc.code} (user_id={user_id}); nothing written")
        return EXIT_REFUSED
    _log(f"{result['status']}: user_id={user_id}")
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="promote_superadmin", description="Grant the platform superadmin flag."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email")
    target.add_argument("--user-id", type=int)
    parser.add_argument(
        "--operator", required=True, help="your identity, recorded in the audit row"
    )
    parser.add_argument("--reason", required=True, help="ticket or incident reference")
    parser.add_argument(
        "--additional",
        action="store_true",
        help="grant even though an active superadmin already exists",
    )
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    args = parser.parse_args(argv)
    if not args.operator.strip():
        parser.error("--operator must not be blank")
    if not args.reason.strip() or len(args.reason) > REASON_MAX_LENGTH:
        parser.error(f"--reason must be 1 to {REASON_MAX_LENGTH} characters")

    try:
        return asyncio.run(_run(args))
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
