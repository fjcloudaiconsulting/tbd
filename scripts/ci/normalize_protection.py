#!/usr/bin/env python3
"""Normalize a GitHub branch-protection payload into a comparable shape (TBD-420).

Reads the raw `GET /repos/{o}/{r}/branches/{b}/protection` document on stdin and
writes the normalized document on stdout. Also importable: `normalize(doc)`.

The normalization is deliberately SHAPE-PRESERVING, never a projection. It drops
only things that are noise by construction (API self-links) and rewrites only
things whose raw shape is an envelope rather than a value. Every other key --
including keys this repo has never seen -- survives into the output, because the
one drift most worth catching is "GitHub shipped a new permissive toggle and it
defaults on". A projection over a hand-listed key set reports green forever in
exactly that case.

⚠⚠ THE FOUR PASSES RUN IN THIS ORDER AND THE ORDER IS LOAD-BEARING:

    1. strip `url` / `*_url`
    2. collapse `{"enabled": <bool>}` envelopes to the bool
    3. reduce `users` / `teams` / `apps` members to `{id, login|slug|name}`
    4. sort EVERY list

`enforce_admins` arrives as `{"url": ..., "enabled": true}` -- a TWO-key dict.
Pass 2 fires only on a dict whose key set is exactly `{"enabled"}`, so running it
before pass 1 leaves `enforce_admins` a dict and the posture grows a shape nobody
intended. That bug was written during authoring.

⚠ Pass 2 is deliberately EXACT-KEY-SET, not `"enabled" in d`. A loose collapse
would return the bool and silently DISCARD every sibling key -- which is the
projection blindness above, reintroduced one level down. If GitHub ever ships
`{"url": ..., "enabled": true, "some_new_toggle": true}`, the exact form keeps
`some_new_toggle` visible (as a two-key dict after the url strip) and the
comparison goes `drifted`, which is the point.
"""

from __future__ import annotations

import json
import sys

MEMBER_LIST_KEYS = ("users", "teams", "apps")

# ⚠⚠ GitHub identifies users by `login` but teams and apps by `slug`. "Reduce to
# sorted LOGINS" is wrong on fact, and wrong in the one sub-object that grants
# bypass: `x.get("login")` maps every app to None, so swapping
# `bypass_pull_request_allowances.apps` from one app to a DIFFERENT one compares
# `[None] == [None]` and reports green. `x["login"]` instead raises KeyError and
# makes the probe a permanent could-not-run.
IDENTITY_KEYS = ("login", "slug", "name")


def _strip_urls(node):
    """Pass 1. Remove API self-links, which change shape across API versions and
    carry no posture information."""
    if isinstance(node, dict):
        return {
            k: _strip_urls(v)
            for k, v in node.items()
            if not (k == "url" or k.endswith("_url"))
        }
    if isinstance(node, list):
        return [_strip_urls(v) for v in node]
    return node


def _collapse_enabled(node):
    """Pass 2. `{"enabled": <bool>}` -> `<bool>`, exact key set only."""
    if isinstance(node, dict):
        collapsed = {k: _collapse_enabled(v) for k, v in node.items()}
        if set(collapsed) == {"enabled"} and isinstance(collapsed["enabled"], bool):
            return collapsed["enabled"]
        return collapsed
    if isinstance(node, list):
        return [_collapse_enabled(v) for v in node]
    return node


def _identity(member):
    """Reduce one grantee to `{id, <name>}`.

    ⚠⚠ NOT to a bare name. Keeping `id` means a RELEASED-AND-RECLAIMED username
    -- a different account wearing the same string -- is caught, because the id
    changes. A plain rename then shows up as an honest one-line posture diff
    rather than a silent change of who can bypass review.

    ⚠⚠ AND NOT `login` ALONE. GitHub identifies users by `login` but teams and
    apps by `slug`. `x["login"]` raises KeyError on an app (a permanent
    could-not-run); `x.get("login")` maps every team and app to None, so swapping
    `bypass_pull_request_allowances.apps` from one app to a DIFFERENT one
    compares `[None] == [None]` and reports GREEN -- the promise false in the one
    sub-object that grants bypass.
    """
    if not isinstance(member, dict):
        return member
    out = {}
    if "id" in member:
        out["id"] = member["id"]
    for key in IDENTITY_KEYS:
        value = member.get(key)
        if isinstance(value, str):
            out[key] = value
            return out
    # ⚠ Do NOT fall back to "" or drop it. An unidentifiable grantee must stay
    # visible in the diff -- dropping it is how a bypass allowance gets granted
    # without the posture changing.
    out["unidentified"] = json.dumps(member, sort_keys=True)
    return out


def _reduce_members(node):
    """Pass 3. Member objects -> sorted identity strings, so an unrelated
    profile edit (avatar url, id renumbering) is not reported as drift."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in MEMBER_LIST_KEYS and isinstance(v, list):
                # Ordering is applied by the sort pass, which handles dicts.
                out[k] = [_identity(m) for m in v]
            else:
                out[k] = _reduce_members(v)
        return out
    if isinstance(node, list):
        return [_reduce_members(v) for v in node]
    return node


def _sort_lists(node):
    """Pass 4. Order-normalize EVERY list.

    ⚠ `required_status_checks.checks` and `.contexts` have no guaranteed order.
    An unsorted comparison therefore alarms on a GitHub reordering with nothing
    actually changed -- a false `drifted` from the one probe whose entire value
    is its credibility. Restricting this to the member lists (revision 1) left
    exactly those two exposed.

    The key is the canonical JSON of the element, which is total and stable for
    strings, numbers and objects alike, so a list of `{context, app_id}` dicts
    orders deterministically without inventing a domain-specific sort field.
    """
    if isinstance(node, dict):
        return {k: _sort_lists(v) for k, v in node.items()}
    if isinstance(node, list):
        return sorted((_sort_lists(v) for v in node),
                      key=lambda v: json.dumps(v, sort_keys=True))
    return node


def normalize(doc):
    return _sort_lists(_reduce_members(_collapse_enabled(_strip_urls(doc))))


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("normalize_protection: empty input", file=sys.stderr)
        return 2
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        print(f"normalize_protection: input is not valid JSON ({exc})", file=sys.stderr)
        return 2
    json.dump(normalize(doc), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
