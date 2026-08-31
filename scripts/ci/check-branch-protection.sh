#!/usr/bin/env bash
# Decide whether `main`'s branch protection still matches the recorded posture
# (TBD-420).
#
# Reads the raw `GET /repos/{o}/{r}/branches/main/protection` payload on STDIN
# and writes a verdict to stdout. The FIRST stdout line is exactly one of the
# three verdict tokens; everything after it is for a human.
#
#   exit 0  in-posture     live == posture, both directions
#   exit 1  drifted        a real difference, or `main` is not protected at all
#   exit 2  could-not-run  the probe cannot answer
#
# Inputs (all env, so every branch is fixture-drivable with no credential):
#   BRANCH_PROTECTED      the ONE bit `.protected` from GET /branches/main
#   EFFECTIVE_RULES_JSON  body of GET /repos/{o}/{r}/rules/branches/main
#   POSTURE_FILE          path to .github/branch-protection/main.json
#
# ⚠ WHAT GREEN MEANS, EXACTLY: the normalized fields of main's CLASSIC branch
# protection equal a committed file. It does NOT mean "nothing about main's
# protection changed". `allow_merge_commit` / `allow_rebase_merge` /
# `allow_squash_merge` live on `GET /repos/{o}/{r}`, NOT here, so re-enabling
# merge commits leaves this probe green while breaking the release pipeline --
# the squash subject IS the string semantic-release parses. A monitor that
# overstates its coverage is worse than none, because the next design decision
# will cite it.
#
# ⚠ STDIN RATHER THAN CALLING `gh` ITSELF, per check-backup-freshness.sh's own
# header: a probe exercisable only against healthy live state proves nothing
# about its unhealthy paths, and those are the only paths that matter.
#
# ⚠⚠ `.protected == false` IS DECIDED FIRST, AHEAD OF EVERY OTHER GUARD. When it
# sat behind the posture-file and rules guards, an unparseable posture -- or a
# transient 502 on the rules fetch -- reported a completely NAKED `main` as
# "the probe is a bit unwell". That is a fail-open on the top-severity state, in
# the one check whose whole purpose is to not fail open on it. `.protected` is
# independently sourced and readable without admin, so nothing may mask it.
#
# ⚠ `BRANCH_PROTECTED` IS TRI-STATE: true / false / unknown (our own fetch can
# fail). The guard is `== "false"` and NEVER `!= "true"`; the latter reads as an
# equivalent refactor while turning "our fetch flaked" into a drift alarm.
#
# ⚠ `.protected` IS NOT A CLASSIC-PROTECTION BIT -- it reads true under a ruleset
# too. It is a one-bit "is anything protecting this branch" oracle and never a
# source for posture fields. In particular this must never read
# `protection.required_status_checks.enforcement_level`, which IS
# `enforce_admins`: that would buy one field and silently sell the other ten.
#
# ⚠ NO STATUS CODE IS CONSULTED, deliberately. 404 is what an unprotected branch
# returns AND what an under-permissioned token returns, and for a GitHub App a
# missing permission commonly surfaces as `403 Resource not accessible by
# integration`. The code does not separate the cases in either direction; the
# independent boolean does.
#
# ⚠⚠ THE RULES ARRAY HAS EXACTLY ONE ARM, AND IT SAYS "RETIRE THIS PROBE".
# It is NEVER a data source: both rule views return `[]` on this repo, so a probe
# sourced from them reports "no problems" forever while classic `enforce_admins`
# sits disarmed. And it is NEVER a premise guard: treating any non-empty
# `rules/branches/main` as could-not-run was a designed-in PERMANENT ALARM,
# because a ruleset is ADDITIVE -- classic protection survives, the comparison
# stays valid, and an operator legitimately hardening the repo would have
# produced could-not-run on every push plus the daily cron.
#
# The one arm that remains: `/protection` UNREADABLE + `.protected == true` +
# rules non-empty means classic protection is GONE and main is governed by
# rulesets this probe cannot read. That is a persistent alarm on the migration
# path and it is CORRECT -- the probe has become unable to answer its question,
# so it says so and names the remedy rather than shrugging.
#
set -euo pipefail

POSTURE_FILE="${POSTURE_FILE:-}"
BRANCH_PROTECTED="${BRANCH_PROTECTED:-}"
EFFECTIVE_RULES_JSON="${EFFECTIVE_RULES_JSON:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || {
  echo "could-not-run"
  echo "python3 is missing on this runner." >&2
  exit 2
}

# ⚠ The evaluator is written to a temp file and run as `python3 FILE`, NEVER as
# `python3 - <<PY`. With `-` the heredoc BECOMES stdin, so the piped protection
# payload would never reach the program and every input -- healthy or not --
# would be judged "empty". That exact defect shipped once in
# check-backup-freshness.sh and was caught only because its branches are
# exercised behaviourally.
PROG="$(mktemp)"
trap 'rm -f "$PROG"' EXIT

cat > "$PROG" <<'PY'
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
from normalize_protection import normalize  # noqa: E402


# ⚠⚠ AN UNCAUGHT PYTHON EXCEPTION EXITS 1, AND THIS SCRIPT'S OWN CONTRACT
# DEFINES 1 AS `drifted`. A crash -- a shape nobody anticipated, a recursion
# limit, an OSError -- would therefore be reported as a CONFIRMED FLOOR CHANGE
# and open an incident issue naming a drift that did not happen. Worse, the
# operator's trained response to a false drift is to loosen the probe. Anything
# unexpected is by definition "could not answer", which is 2.
def _crash(exc_type, exc, tb):
    sys.stdout.write("could-not-run\n")
    sys.stdout.write(
        f"the checker crashed unexpectedly ({exc_type.__name__}: {exc}). This is "
        "reported as could-not-run rather than drifted: exit 1 means a confirmed "
        "floor change and a crash confirms nothing.\n")
    sys.stdout.flush()
    os._exit(2)


sys.excepthook = _crash

def verdict(token, message, code):
    print(token)
    print(message)
    raise SystemExit(code)


def cnr(message):
    verdict("could-not-run", message, 2)


def rules_state():
    """Tri-state: True (rules exist), False (none), None (could not tell).

    ⚠ A collapsed boolean loses the remedy. `GET /rules/branches/main` can return
    an ERROR ENVELOPE -- a dict, not a list -- and a two-valued helper reports
    that as "no rules", so the ruleset-MIGRATION arm silently degrades to the
    generic credential message. The severity is the same either way, but the
    operator is told the wrong thing to do, and "retire or replace this probe" is
    not a conclusion they will reach on their own.
    """
    raw = os.environ.get("EFFECTIVE_RULES_JSON", "")
    if not raw.strip():
        return None
    try:
        rules = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(rules, list):
        return None
    return bool(rules)


# ⚠ stdin is read here, BEFORE the first decision, only so that the naked-main
# message can describe what the other read actually saw. It is not consulted for
# the verdict at this point -- the ordering is unchanged.
raw = sys.stdin.read()

# --- 1. is main protected at all? Decided before anything else can mask it. ---
protected = os.environ.get("BRANCH_PROTECTED", "")
if protected == "false":
    # ⚠ THE TWO READS USE DIFFERENT CREDENTIALS AND DIFFERENT ENDPOINTS, so they
    # can disagree, and an earlier version of this message asserted "no other
    # input can change that answer" while a readable protection document sat on
    # stdin unexamined. The verdict and the ordering are right; the sentence was
    # overclaiming. Report the disagreement instead of denying it.
    if raw.strip():
        detail = (
            "main reports NOT PROTECTED. `.protected` is false on the "
            "repository-metadata read.\n"
            "  ⚠ The two reads DISAGREE: a protection document was also readable "
            "on the admin credential. They use different endpoints and different "
            "credentials, so this is a real state, not an impossibility. Treat "
            "main as unprotected until the disagreement is explained -- an "
            "absent floor is the higher-severity reading and this probe will not "
            "resolve it in favour of the safer one."
        )
    else:
        detail = (
            "main is NOT PROTECTED AT ALL. `.protected` is false and no "
            "protection document was readable: there is no floor on main."
        )
    verdict("drifted", detail, 1)

# --- 2. the posture file ------------------------------------------------------
posture_path = os.environ.get("POSTURE_FILE", "")
if not posture_path:
    cnr("POSTURE_FILE is not set, so there is nothing to compare against.")
try:
    with open(posture_path, "r", encoding="utf-8") as fh:
        posture = json.load(fh)
except OSError as exc:
    cnr(f"the posture file {posture_path!r} could not be read ({exc}).")
except ValueError as exc:
    cnr(f"the posture file {posture_path!r} is not valid JSON ({exc}).")

if not isinstance(posture, dict):
    cnr(f"the posture file {posture_path!r} is not a JSON object.")

if not posture:
    cnr("the posture file records no fields at all.")

# --- 3. the payload itself ----------------------------------------------------
if not raw.strip():
    # ⚠ THE ONE RULES ARM. `.protected` is true, so SOMETHING governs main, but
    # the classic protection document is unreadable and branch rules exist: the
    # floor's representation changed and this artifact can no longer read it.
    rules = rules_state()
    if protected == "true" and rules is True:
        cnr(
            "classic branch protection is GONE; `main` is now governed by "
            "RULESETS, which this probe cannot read. This is not a transient "
            "failure and it will not clear on its own -- RETIRE OR REPLACE this "
            "probe and its posture file with a ruleset-aware equivalent."
        )
    unknown_rules = (
        "\n  ⚠ The effective-rules view was also unreadable, so a migration to "
        "RULESETS cannot be ruled out. If it has happened, this probe must be "
        "retired or replaced rather than repaired." if rules is None else "")
    cnr(
        "the /protection payload on stdin is empty, so the protection document "
        f"could not be read. `.protected` is {protected!r}, so this reads as a "
        "credential or availability problem rather than an absent floor."
        + unknown_rules
    )
try:
    live = json.loads(raw)
except ValueError as exc:
    cnr(f"the /protection payload on stdin is not valid JSON ({exc}).")
if not isinstance(live, dict):
    cnr("the /protection payload on stdin is not a JSON object.")

live = normalize(live)

# ⚠ A 200 carrying a GitHub error envelope, or `{}`, parses fine and shares NO
# keys with the posture. Comparing it would emit `drifted` with a diff naming
# every field at once -- technically true and operationally indistinguishable
# from noise. Zero overlap means "this is not a protection payload". ONE
# overlapping key is enough to proceed, so this cannot mask real field loss.
if not (set(live) & set(posture)):
    cnr(
        "the payload on stdin shares no fields with the posture, so it is not a "
        f"protection document. It had keys {sorted(live)[:6]}."
    )

# --- 4. strict equality, both directions --------------------------------------
missing = sorted(k for k in posture if k not in live)
extra = sorted(k for k in live if k not in posture)
changed = sorted(k for k in set(posture) & set(live) if posture[k] != live[k])

if missing or extra or changed:
    lines = ["main's protection no longer matches the recorded posture."]
    if extra:
        lines.append(
            f"  present live, absent from the posture: {extra} "
            "(a new toggle nobody has ratified)"
        )
    if missing:
        lines.append(f"  in the posture, absent live: {missing} (a field was removed)")
    for key in changed:
        lines.append(f"  {key}: posture {posture[key]!r} -> live {live[key]!r}")
    lines.append(
        "Nothing regenerates .github/branch-protection/main.json. Read the "
        "difference, then either restore the setting or regenerate the posture: "
        "gh api repos/:owner/:repo/branches/main/protection | "
        "python3 scripts/ci/normalize_protection.py > "
        ".github/branch-protection/main.json"
    )
    verdict("drifted", "\n".join(lines), 1)

verdict(
    "in-posture",
    "main's classic branch protection matches the recorded posture, field for "
    "field, in both directions. See .github/branch-protection/README.md for what "
    "that does and does not cover.",
    0,
)
PY

python3 "$PROG" "$HERE"
