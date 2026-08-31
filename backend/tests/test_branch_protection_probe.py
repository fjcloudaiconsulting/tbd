"""Fences for the branch-protection posture probe (TBD-420, spec revision 3).

The hazard is not "a flag drifted". It is that `main`'s protection had **no
recorded intended state**, so no reading of it could be classified as drift or as
intent. Two fields drifted six days apart and both were found by accident; the
second, `allow_force_pushes: true`, sat unnoticed for at least three weeks while
the ticket's own grooming comment pasted a FOUR-FIELD projection of the payload
and concluded the floor was healthy. That projection is what hid it.

So the load-bearing property is that the comparison is **total in both
directions**, and the load-bearing fence is F20 -- because F2/F3/F4 all drive the
checker from stdin, and the projection defect is fully reintroducible one layer
UP, in the fetch, with every one of them still green.

⚠ Every textual assertion strips comment lines first. Both scripts document, at
length, the endpoints and verbs a whole-file grep would look for -- the checker's
own header names the rulesets trap while explaining why those endpoints must
never be a data source. A grep over the raw file is satisfied by its own
explanation.

⚠ ON IMPORTS. The spec says this module must import "nothing beyond stdlib +
pytest". That is an over-generalisation of the real finding and is not followed
literally: `yaml` is imported, exactly as ten sibling fence modules already do
(`test_deploy_drift_probe.py`, `test_ci_change_scoping_workflow.py`, ...). The
actual finding was that the module must not depend on `backend/tests/conftest.py`
-- whose autouse fixture imports `structlog` -- because a missing conftest
dependency turns every fence into a setup ERROR that reads like a RED while
proving nothing. Nothing here uses a conftest fixture. Forbidding `yaml` would
make F6/F7/F14/F15/F21 unwritable as *parsing* fences, and F6 explicitly forbids
the raw-string form that would replace them.
"""

import json
import os
import pathlib
import re
import subprocess

import pytest
import yaml


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "branch-protection" / "main.json").exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        ".github/branch-protection/main.json not found from a CI checkout; these "
        "fences must not be allowed to skip on the runner."
    )

_CONTAINER_SCRIPTS = pathlib.Path("/app/repo-scripts")


def _artifact(relpath: str) -> pathlib.Path:
    """Resolve a repo-root artifact in either layout.

    ⚠ `/app/scripts` inside the backend container is the BACKEND's own scripts
    package, not the repo-root `scripts/`; the repo-root one is mounted at
    `/app/repo-scripts` (docker-compose.yml). This RAISES with the remedy rather
    than skipping, following test_deploy_drift_probe.py: a skip makes a fence
    silently absent in whichever environment lacks the path, and a false red is
    what gets a fence weakened rather than obeyed.
    """
    if REPO_ROOT is not None:
        candidate = REPO_ROOT / relpath
        if candidate.is_file():
            return candidate
    if relpath.startswith("scripts/"):
        alt = _CONTAINER_SCRIPTS / relpath[len("scripts/"):]
        if alt.is_file():
            return alt
    raise RuntimeError(
        f"Could not locate {relpath}. On a checkout it sits at the repo root; in "
        "the backend container repo-root scripts/ is mounted read-only at "
        "/app/repo-scripts and .github at /app/.github (docker-compose.yml). A "
        "container built before those mounts existed shows this module red -- run "
        "`docker compose up -d --force-recreate backend` once, and do NOT weaken "
        "this fence."
    )


def _scripts_dir() -> pathlib.Path:
    """Repo-root ``scripts/``, honouring the backend container's mount layout.

    ⚠⚠ MEASURED: four fences resolved this as ``REPO_ROOT / "scripts"`` and were
    RED (or worse, green-for-the-wrong-reason) inside the backend container.
    ``/app/scripts`` in there is **backend/scripts** -- no ``ci/``, no
    ``notify-*.sh`` -- while the repo-root tree is mounted at
    ``/app/repo-scripts`` (docker-compose.yml). The module already built
    ``_CONTAINER_SCRIPTS`` for exactly this and then four places ignored it.

    The worst case was not a red: ``_run_the_check_step`` ran the workflow's
    shell with ``cwd=REPO_ROOT``, so ``bash scripts/ci/probe-branch-protection.sh``
    resolved to nothing, the verdict came back empty, the auth-override set
    ``could-not-run``, and the assertion matched -- a fence PASSING **without
    ever running the probe**.

    This matters beyond CI: the local command in ``.github/branch-protection/README.md``
    and ``CLAUDE.md`` must work, so the container run is not optional.
    """
    if REPO_ROOT is not None and (REPO_ROOT / "scripts" / "ci").is_dir():
        return REPO_ROOT / "scripts"
    if (_CONTAINER_SCRIPTS / "ci").is_dir():
        return _CONTAINER_SCRIPTS
    raise RuntimeError(
        "Could not locate repo-root scripts/. On a checkout it is <root>/scripts; "
        "in the backend container it is mounted read-only at /app/repo-scripts "
        "(/app/scripts is backend's own package). Run "
        "`docker compose up -d --force-recreate backend` once."
    )


def _scripts_dir_is_the_repo_root_one() -> bool:
    d = _scripts_dir()
    return (d / "ci" / "check-branch-protection.sh").is_file()


def test_the_scripts_resolver_finds_the_repo_root_tree_not_backends():
    """Non-vacuity of the resolver every corpus fence now depends on. If it ever
    returned ``backend/scripts`` the corpora would silently shrink to nothing and
    the fences below would pass by searching an empty set."""
    d = _scripts_dir()
    assert (d / "ci" / "check-branch-protection.sh").is_file(), d
    assert (d / "notify-protection-drift.sh").is_file(), d
    assert not (d / "migrate.py").is_file(), (
        f"{d} is backend/scripts, not the repo-root scripts/")


POSTURE = ".github/branch-protection/main.json"
CHECK = "scripts/ci/check-branch-protection.sh"
PROBE = "scripts/ci/probe-branch-protection.sh"
NORMALIZER = "scripts/ci/normalize_protection.py"
NOTIFIER = "scripts/notify-protection-drift.sh"
WORKFLOW = ".github/workflows/branch-protection-probe.yml"

# ⚠ F11 is SCOPED TO THE NEW FILES BY NAME. `breakglass-merge.sh:35,57`
# legitimately POSTs and DELETEs that exact protection path, so an
# over-broad repo scan would be red on day one -- and a fence that is red on
# day one gets weakened, not obeyed.
NEW_ARTIFACTS = (CHECK, PROBE, NORMALIZER, NOTIFIER)


def _code_lines(path: pathlib.Path) -> list[str]:
    """Executable lines only, with Python DOCSTRINGS removed.

    Comment-stripping alone is not enough here: a `.py` file's module docstring
    is prose that a grep reads as code. `ast` separates them properly, which is
    the repo's own "parse, never grep" rule applied to its own fence.
    """
    text = path.read_text()
    skip: set[int] = set()
    if path.suffix == ".py":
        import ast
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    skip.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if i in skip:
            continue
        s = line.strip()
        if s.startswith("#"):
            continue
        out.append(s)
    return out


def _executable_lines(relpath: str) -> list[str]:
    return [
        ln for ln in _artifact(relpath).read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]


def _load(relpath: str, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, _artifact(relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalize_module():
    return _load(NORMALIZER, "normalize_protection_under_test")


# ---------------------------------------------------------------------------
# The raw payload GitHub actually emits. Normalizing it must reproduce the
# seeded posture EXACTLY.
#
# ⚠⚠ A FABRICATED FIXTURE THAT DOES NOT MATCH WHAT THE REAL PRODUCER EMITS IS
# NOT A TEST OF THAT PRODUCER -- the backup fence learned this by shipping a
# verifier that passed every test and then refused every real backup. The
# envelope shapes below (`{"url": ..., "enabled": bool}` for the toggles, member
# OBJECTS rather than logins, an app object carrying `slug` and NO `login`, a
# `contexts_url` beside `contexts`) are what make the normalizer's four passes
# necessary at all. Flatten them and every normalizer fence goes vacuous.
# ---------------------------------------------------------------------------
API = "https://api.github.com/repos/flamarion/tbd/branches/main/protection"


def _raw_live() -> dict:
    return {
        "url": API,
        "required_status_checks": {
            "url": f"{API}/required_status_checks",
            "strict": False,
            "contexts": ["Backend Checks", "Frontend Checks"],
            "contexts_url": f"{API}/required_status_checks/contexts",
            "checks": [
                {"context": "Backend Checks", "app_id": 15368},
                {"context": "Frontend Checks", "app_id": 15368},
            ],
        },
        "required_pull_request_reviews": {
            "url": f"{API}/required_pull_request_reviews",
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
            "required_approving_review_count": 1,
            "bypass_pull_request_allowances": {
                "users": [
                    # ⚠ REAL account ids, read from the live API. They were
                    # placeholders (1, 2) while this was authored without an
                    # admin credential, and every fence that compares against the
                    # committed posture failed the moment the real posture landed
                    # -- eleven at once, all from THIS one fixture. That is the
                    # right blast radius: the ids live in exactly one place.
                    {"login": "flamarion", "id": 29267749, "type": "User",
                     "avatar_url": "https://avatars.example/1"},
                    {"login": "fjcloudai", "id": 277788689, "type": "User",
                     "avatar_url": "https://avatars.example/2"},
                ],
                "teams": [],
                "apps": [],
            },
        },
        "required_signatures": {"url": f"{API}/required_signatures", "enabled": False},
        "enforce_admins": {"url": f"{API}/enforce_admins", "enabled": True},
        "required_linear_history": {"enabled": False},
        "allow_force_pushes": {"enabled": True},
        "allow_deletions": {"enabled": False},
        "block_creations": {"enabled": False},
        "required_conversation_resolution": {"enabled": False},
        "lock_branch": {"enabled": False},
        "allow_fork_syncing": {"enabled": False},
    }


def _run_check(payload, *, protected="true", rules="[]",
               posture_path=None, posture_obj=None, tmp_path=None):
    """Drive the verdict script. stdout line 1 is the verdict token.

    ⚠ The env surface is THREE names. `PROTECTION_HTTP_STATUS` was subtracted in
    revision 2: `.protected` carries the disambiguation alone, and a status code
    could not have carried it anyway (404 is what an unprotected branch returns
    AND what an under-permissioned token returns; for a GitHub App a missing
    permission commonly surfaces as 403 instead).
    """
    if posture_obj is not None:
        assert tmp_path is not None
        posture_path = tmp_path / "posture.json"
        posture_path.write_text(json.dumps(posture_obj))
    if posture_path is None:
        posture_path = _artifact(POSTURE)
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    return subprocess.run(
        ["bash", str(_artifact(CHECK))],
        input=payload, capture_output=True, text=True,
        env={
            **os.environ,
            "BRANCH_PROTECTED": protected,
            "EFFECTIVE_RULES_JSON": rules,
            "POSTURE_FILE": str(posture_path),
        },
    )


def _verdict(result) -> str:
    return result.stdout.splitlines()[0].strip() if result.stdout.strip() else ""


# ---------------------------------------------------------------------------
# Positive baseline. Without it, a comparator that returns `drifted` for
# EVERYTHING passes most of what follows while asserting nothing.
# ---------------------------------------------------------------------------
def test_the_committed_posture_matches_the_payload_it_was_seeded_from():
    """⚠ The seed is measured live state, not aspiration. An aspirational posture
    (`allow_force_pushes: false`) makes the probe RED on the day it merges, and a
    monitor red from birth is trained into noise before it is ever trusted --
    this ticket's own failure one level up. Green must mean "nothing changed
    since a human last looked", a claim the probe can actually make.

    ⚠ WHAT THIS CAN AND CANNOT PROVE. It shows `normalize` is deterministic over
    the fixture and that the fixture and the committed posture agree. It does NOT
    show the posture matches GitHub -- both sides live in this repo, so only a
    LIVE call can prove that. That was done out of band and is recorded in
    `.github/branch-protection/README.md`:

        gh api .../protection | python3 scripts/ci/normalize_protection.py \
          | diff - .github/branch-protection/main.json    # IDENTICAL
    """
    r = _run_check(_raw_live())
    assert _verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"


# ⚠ NO FENCE ON THE KEY COUNT, deliberately. Revision 1 wrote "sixteen fields",
# revision 2 corrected it to "11 top-level keys" -- a different magic number, in
# the very paragraph explaining why the first one was harmful. A future reader
# fences against whatever number is written down, and then a legitimate new
# GitHub toggle makes that fence red for the wrong reason.


# ---------------------------------------------------------------------------
# F2. Kills: project-and-ignore. A key live but absent from the posture.
# ---------------------------------------------------------------------------
def test_f2_a_new_live_key_absent_from_the_posture_is_drift():
    """The single most valuable thing the COMPARATOR can detect: GitHub ships a
    new permissive toggle, it defaults on, and a projection reports green
    forever. (The FETCH-layer twin of this is F20.)"""
    live = _raw_live()
    live["allow_secret_new_bypass"] = {"enabled": True}
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert "allow_secret_new_bypass" in r.stdout


def test_f2_a_new_nested_live_key_is_drift():
    """Nested, because the toggle that matters most is likelier to arrive inside
    `required_pull_request_reviews` than at the top level, and a comparator total
    at the top level only would miss it."""
    live = _raw_live()
    live["required_pull_request_reviews"]["allow_anyone_to_bypass"] = True
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------
# F3. Kills: naive dict-intersection, which returns EQUAL here.
# ---------------------------------------------------------------------------
def test_f3_a_posture_key_absent_live_is_drift():
    live = _raw_live()
    del live["required_signatures"]
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert "required_signatures" in r.stdout


def test_f3_enforce_admins_vanishing_entirely_is_drift_not_in_posture():
    """The named case. `enforce_admins` absent is the floor being disarmed, and
    an intersection-based comparator calls it `in-posture`."""
    live = _raw_live()
    del live["enforce_admins"]
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert r.returncode == 1
    assert "enforce_admins" in r.stdout


# ---------------------------------------------------------------------------
# F4 / F4b / F4c. Value drift, member reduction, member identity.
# ---------------------------------------------------------------------------
def test_f4_enforce_admins_flipping_false_is_drift():
    live = _raw_live()
    live["enforce_admins"] = {"url": f"{API}/enforce_admins", "enabled": False}
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert r.returncode == 1
    assert "enforce_admins" in r.stdout


def test_f4_a_new_bypass_grantee_is_drift():
    """A third account gaining `bypass_pull_request_allowances` is the change
    most likely to be made quietly through the console UI."""
    live = _raw_live()
    live["required_pull_request_reviews"]["bypass_pull_request_allowances"][
        "users"].append({"login": "someone-else", "id": 3, "type": "User"})
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f4b_an_unrelated_profile_edit_is_not_drift():
    """The inverse defect: a comparator that alarms on everything is not a
    comparator. Members reduce to `{id, name}`, so an AVATAR URL change or a
    reordering must NOT alarm -- otherwise the probe is noise by construction.

    ⚠⚠ THE `id` IS DELIBERATELY NOT MUTATED HERE, and an earlier draft of this
    fence did mutate it. Keeping `id` in the identity is what catches a
    RELEASED-AND-RECLAIMED username, so "an id renumbering must not alarm" and
    "a reclaimed username must alarm" are the SAME assertion with opposite signs.
    Carrying the older, broader wording of this fence forward makes it
    contradict `test_f4c_a_reclaimed_username_is_caught_because_id_is_kept`, and
    the suite cannot be green on both. The narrow reading -- url-ish profile
    fields only -- is the one that holds."""
    live = _raw_live()
    users = live["required_pull_request_reviews"][
        "bypass_pull_request_allowances"]["users"]
    users[0]["avatar_url"] = "https://avatars.example/999"
    users[0]["gravatar_id"] = "deadbeef"
    users.reverse()
    r = _run_check(live)
    assert _verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"


@pytest.mark.parametrize("bucket,key", [
    ("apps", "slug"), ("teams", "slug"), ("users", "login"),
])
def test_f4c_swapping_one_bypass_grantee_for_another_is_drift(bucket, key):
    """⚠⚠ "Reduce to sorted LOGINS" is wrong on fact: team and app objects carry
    `slug`, NOT `login`. A literal implementation either raises KeyError (a
    permanent could-not-run) or maps every team and app to None -- so swapping
    `bypass_pull_request_allowances.apps` from one app to a DIFFERENT one
    compares `[None] == [None]` and reports GREEN. That is the promise being
    false in the one sub-object that grants bypass.

    ⚠ Driven over all THREE buckets. A fence that exercised only `users` passes
    against `x.get("login")` -- users are the one bucket where it works."""
    mod = _normalize_module()
    def grant(name):
        live = _raw_live()
        live["required_pull_request_reviews"]["bypass_pull_request_allowances"][
            bucket] = [{key: name, "id": 99, "name": f"{name} display"}]
        return mod.normalize(live)
    a, b = grant("alpha-grantee"), grant("beta-grantee")
    assert a != b, (
        f"two DIFFERENT bypass {bucket} normalized identically -- the identity "
        "key collapsed them (the `x.get('login')` -> [None] defect)")
    assert "alpha-grantee" in json.dumps(a)


def test_f4b_does_not_mutate_id_and_cannot_be_folded_back_into_doing_so():
    """⚠⚠ A MECHANICAL guard on the F4b/F4c contradiction, not just a comment.

    An earlier revision's F4b mutated `users[0]["id"]` and asserted NOT drift.
    Revision 3 keeps `id` in the identity precisely so a reclaimed username IS
    drift. The two cannot both hold, and carrying the older wording forward is
    the obvious fold -- it was made once here already. A prose warning inside a
    docstring is exactly the thing this repo has learned a reader skips, so the
    invariant is asserted over F4b's own source.
    """
    import inspect
    src = inspect.getsource(test_f4b_an_unrelated_profile_edit_is_not_drift)
    body = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#") and '"""' not in ln)
    body = body.split('"""')[-1]
    assert '["id"]' not in body and "'id'" not in body, (
        "F4b mutates `id`. `id` is part of the member identity, so that makes it "
        "assert the exact opposite of "
        "test_f4c_a_reclaimed_username_is_caught_because_id_is_kept, and the "
        "suite cannot be green on both. F4b must mutate url-ish profile fields "
        "only (avatar_url, gravatar_id).")
    assert "avatar_url" in body, "F4b no longer exercises a profile field at all"


def test_f4c_a_reclaimed_username_is_caught_because_id_is_kept():
    """⚠ Members reduce to `{id, login|slug|name}`, not to a bare name. A
    RELEASED-AND-RECLAIMED username is a different account wearing the same
    string; keeping `id` catches it. A plain rename then shows up as an honest
    one-line posture diff rather than a silent change of who can bypass."""
    mod = _normalize_module()
    def user(uid):
        live = _raw_live()
        live["required_pull_request_reviews"]["bypass_pull_request_allowances"][
            "users"] = [{"login": "flamarion", "id": uid, "type": "User"}]
        return mod.normalize(live)
    assert user(1) != user(99999), (
        "the same login on a DIFFERENT account id normalized identically -- a "
        "reclaimed username would silently keep its bypass grant")


def test_f4c_a_grantee_with_no_known_identity_key_is_not_dropped():
    """An unidentifiable grantee falls back to its JSON, never to being dropped.
    Dropping is how a bypass allowance gets added without the posture changing.
    """
    mod = _normalize_module()
    out = mod.normalize({"users": [{"unexpected_shape": "abc", "id": 7}]})
    assert out["users"] and "abc" in json.dumps(out["users"]), out


# ---------------------------------------------------------------------------
# F5. Kills: could-not-run reported as all-clear. Plus the shape guard.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,label", [
    ("", "empty stdin"),
    ("   \n", "whitespace only"),
    ("{}", "an empty object"),
    ("not json at all", "non-JSON"),
    ('{"message": "Not Found", "status": "404"}', "a GitHub error envelope"),
    ('[{"enforce_admins": true}]', "a JSON array"),
    ("null", "JSON null"),
])
def test_f5_unanswerable_input_is_exit_2_never_0(payload, label):
    """⚠ The shape guard is required and is not the same as parse failure. A 200
    carrying `{"message":"Not Found"}` PARSES FINE and would otherwise compare as
    `drifted`, naming every field at once -- an alarm that is technically true
    and operationally indistinguishable from noise."""
    r = _run_check(payload)
    assert r.returncode == 2, f"{label}: exit {r.returncode}\n{r.stdout}{r.stderr}"
    assert _verdict(r) == "could-not-run", f"{label}: {r.stdout}{r.stderr}"


def test_f5_one_overlapping_key_is_enough_to_proceed():
    """The shape guard must not over-fire. One overlapping key means this IS a
    protection payload that has lost fields, which is drift, not confusion."""
    r = _run_check({"enforce_admins": {"enabled": False}})
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f5_a_missing_posture_file_is_exit_2(tmp_path):
    r = _run_check(_raw_live(), posture_path=tmp_path / "absent.json")
    assert r.returncode == 2, f"{r.stdout}{r.stderr}"
    assert _verdict(r) == "could-not-run"


def test_f5_an_unparseable_posture_file_is_exit_2(tmp_path):
    bad = tmp_path / "posture.json"
    bad.write_text("{ this is not json")
    r = _run_check(_raw_live(), posture_path=bad)
    assert r.returncode == 2, f"{r.stdout}{r.stderr}"
    assert _verdict(r) == "could-not-run"


# ---------------------------------------------------------------------------
# F9 / F9b. THE reordering fence. Naked main outranks every other guard.
# ---------------------------------------------------------------------------
def test_f9_naked_main_is_drifted_even_when_everything_else_failed(tmp_path):
    """⚠⚠ THE fence of revision 2's reordering. In revision 1 the `.protected`
    step sat BEHIND the posture-file and rules guards, so an unparseable posture
    -- or a transient 502 on the rules fetch -- reported a completely NAKED
    `main` as "the probe is a bit unwell". That is verbatim the fail-open the
    step exists to prevent, reintroduced one step earlier.

    `.protected` is independently sourced and readable without admin, so nothing
    may mask it. Here EVERY other input is broken at once."""
    unreadable = tmp_path / "nope.json"
    unreadable.write_text("{ not json")
    r = _run_check("", protected="false", rules="<502 Bad Gateway>",
                   posture_path=unreadable)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert r.returncode == 1, f"exit {r.returncode}\n{r.stdout}{r.stderr}"
    assert "not protected" in r.stdout.lower()


def test_f9_naked_main_is_drifted_with_a_missing_posture_file(tmp_path):
    r = _run_check("", protected="false", posture_path=tmp_path / "absent.json")
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert r.returncode == 1


def test_f9_naked_main_is_drifted_on_a_healthy_run_too():
    r = _run_check("", protected="false")
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f9b_an_unknown_protected_bit_never_reports_drift():
    """⚠ `.protected` is TRI-STATE, not a bit: our own `/branches/main` fetch can
    fail. The guard must be `== "false"`, NEVER `!= "true"` -- the latter reads as
    an equivalent refactor while turning "our fetch flaked" into a drift alarm,
    and a probe that cries wolf when its own fetch flakes is trained into noise
    on schedule."""
    for unknown in ("", "null", "  ", "error"):
        r = _run_check("", protected=unknown)
        assert _verdict(r) == "could-not-run", f"{unknown!r}: {r.stdout}{r.stderr}"
        assert r.returncode == 2, f"{unknown!r}: exit {r.returncode}"


def test_f9b_an_unknown_protected_bit_with_a_good_payload_still_answers():
    """⚠ UNDER-SPECIFIED IN THE SPEC, pinned here. If `/protection` returned a
    full payload equal to the posture, protection demonstrably exists and is
    correct, so the truthful answer is `in-posture` -- even though the
    independent `.protected` read flaked. The alternative (any flake on the
    second fetch ⇒ could-not-run) makes a healthy repo alarm on transient API
    noise, which is the failure mode this spec calls fatal everywhere else."""
    r = _run_check(_raw_live(), protected="")
    assert _verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------
# F10 / F10b. Rulesets: informational, never a source, never a premise guard.
# ---------------------------------------------------------------------------
RULE = {"type": "pull_request", "ruleset_id": 7,
        "ruleset_source_type": "Organization", "ruleset_source": "FlamaCorp"}


def test_f10b_a_ruleset_appearing_does_not_stop_the_probe_answering():
    """⚠⚠ Kills revision 1's premise guard, which was a DESIGNED-IN PERMANENT
    ALARM. It made any non-empty `rules/branches/main` a `could-not-run`. GitHub
    is actively steering repos toward rulesets, and a ruleset is ADDITIVE --
    classic protection survives and the comparison stays perfectly valid. So the
    operator legitimately HARDENING the repo would have produced `could-not-run`
    on every push to `main` plus the daily cron, commenting forever on a
    never-auto-closed issue, resolvable only by editing the probe."""
    r = _run_check(_raw_live(), rules=json.dumps([RULE]))
    assert _verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"
    assert r.returncode == 0


def test_f10b_the_ruleset_migration_arm_says_retire_this_probe():
    """⚠ The ONE surviving rules arm. `/protection` unreadable + `.protected`
    true + rules non-empty means classic protection is GONE and main is governed
    by rulesets this probe cannot read. That is a persistent alarm on the
    migration path and it is CORRECT: the probe has become unable to answer its
    question, and the message must name the remedy rather than merely shrug.

    ⚠ Revision 2's "informational line" is subtracted -- it was written to the
    stdout of a GREEN job that notifies nobody, which is worse than a red
    square."""
    r = _run_check("", protected="true", rules=json.dumps([RULE]))
    assert _verdict(r) == "could-not-run", f"{r.stdout}{r.stderr}"
    assert r.returncode == 2
    low = r.stdout.lower()
    assert "ruleset" in low and ("retire" in low or "replace" in low), (
        f"the migration arm must name the remedy, not just shrug:\n{r.stdout}")


def test_f10b_a_ruleset_does_not_mask_real_drift():
    live = _raw_live()
    live["enforce_admins"] = {"enabled": False}
    r = _run_check(live, rules=json.dumps([RULE]))
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f10_the_rules_array_can_never_rescue_a_drifted_verdict():
    """Behavioural, and it is the half that matters -- the structural grep is
    near-tautological because the checker fetches nothing. Both rule views return
    `[]` here, so a probe reading its verdict from them reports "no problems"
    forever while classic `enforce_admins` sits disarmed: a MEASURED
    false-all-clear trap. Feed a rules array that literally CONTAINS the posture.
    """
    posture = json.loads(_artifact(POSTURE).read_text())
    live = _raw_live()
    live["enforce_admins"] = {"enabled": False}
    r = _run_check(live, rules=json.dumps([posture]))
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert r.returncode == 1


def test_f10_an_unreadable_rules_view_does_not_stop_the_comparison():
    """A readable /protection payload answers the question regardless of what the
    rules fetch did. Kills "unreadable rules -> could-not-run", revision 1's
    permanent-alarm arm."""
    for broken in ("", "   ", "not json", "{}", "<html>502</html>"):
        r = _run_check(_raw_live(), rules=broken)
        assert _verdict(r) == "in-posture", f"{broken!r}: {r.stdout}{r.stderr}"


def test_f10_the_checker_never_mentions_rulesets_outside_its_own_warning():
    """The ONE structural rules fence that is kept. A sibling asserting the rules
    array is never indexed into was subtracted: no mutant died to it, and the
    behavioural fence above does all the work. Two near-tautological greps are
    not twice the evidence of one.

    ⚠ Comment lines are stripped FIRST. Both scripts name these endpoints
    while explaining why they must never be a source, so a whole-file grep is
    satisfied by its own explanation."""
    for rel in (CHECK, PROBE):
        # ⚠ CASE-SENSITIVE MATCHING WOULD MAKE THIS RED ON A CORRECT
        # IMPLEMENTATION. The checker's own migration message shouts "RULESETS",
        # and the suite was green only because of that capitalisation -- lowering
        # one word in a user-facing string would have failed a fence about
        # endpoint usage. Match the ENDPOINT path, not the English word.
        body = "\n".join(_executable_lines(rel))
        assert "/rulesets" not in body and "rulesets?" not in body, (
            f"{rel} references the /rulesets endpoint outside a comment. It "
            "lists repo-level rules only and is blind to org-level ones."
        )


# ---------------------------------------------------------------------------
# F11. Kills: a mutating probe -- INCLUDING the `gh api` field-flag bypass.
# ---------------------------------------------------------------------------
# ⚠ ATTACHED SHORTHAND AND CASE. Measured: `-XDELETE` (valid pflag syntax, and
# `gh` accepts it) and `-X delete` both evaded the original
# `(?:-X|--method)[=\s]+(PUT|POST|DELETE|PATCH)`. The separator is optional for
# the short form and the verb is not case-sensitive.
METHOD_FLAGS = re.compile(r"(?:-X|--method)[=\s]*(PUT|POST|DELETE|PATCH)\b", re.I)
FIELD_FLAGS = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:[=\s]|$)")
PROTECTION_PATH = re.compile(r"branches/[^\s\"']*/protection")

# A write whose TARGET is the posture file. Anchored on the target so that a
# label, a filename or an unrelated `2>&1` on the same line cannot trip it.
WRITES_POSTURE = re.compile(
    r"(?:>>?|(?:tee|cp|mv)\s+(?:-\w+\s+)?(?:\S+\s+)?)\s*\S*branch-protection/main\.json"
)


def _probe_path_logical_lines() -> dict[str, list[str]]:
    """Every file on the probe path, comment-stripped, continuations JOINED."""
    out = {rel: _logical_lines(rel) for rel in NEW_ARTIFACTS}
    wf_lines = [ln for ln in _artifact(WORKFLOW).read_text().splitlines()
                if not ln.lstrip().startswith("#")]
    joined, buf = [], ""
    for line in wf_lines:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        joined.append((buf + stripped).strip())
        buf = ""
    if buf:
        joined.append(buf.strip())
    out[WORKFLOW] = joined
    return out


def _probe_path_files() -> dict[str, str]:
    blobs = {rel: "\n".join(_executable_lines(rel)) for rel in NEW_ARTIFACTS}
    blobs[WORKFLOW] = "\n".join(
        ln for ln in _artifact(WORKFLOW).read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    return blobs


def test_f11_nothing_on_the_probe_path_uses_a_mutating_method():
    """Auto-remediation is explicitly subtracted: it needs `Administration:
    write`, making the probe the very unenumerated mutation path it exists to
    expose, and self-healing masks the drift it should report."""
    offenders = []
    for name, lines in _probe_path_logical_lines().items():
        for line in lines:
            if METHOD_FLAGS.search(line):
                offenders.append(f"{name}: {line[:120]}")
    assert not offenders, f"the probe must be READ-ONLY. Found: {offenders}"


def test_f11_the_gh_api_field_flag_bypass_is_closed():
    """⚠⚠ THE LIVE BYPASS in revision 1's fence. `gh api` defaults to **POST**
    whenever any `-f`/`-F`/`--field`/`--raw-field`/`--input` is present, with no
    `-X` anywhere. So

        gh api repos/O/R/branches/main/protection/enforce_admins -f _=""

    is a real mutation that an `-X`-only matcher does not see -- and that exact
    endpoint is the one `breakglass-merge.sh` already POSTs to."""
    offenders = []
    # ⚠⚠ LOGICAL lines, not physical ones. This is the EXACT defect already
    # measured and fixed for F20 in this same module: every real `gh api` call in
    # `probe-branch-protection.sh` is written in continuation style, so a mutant
    # would be too -- and a physical-line matcher never sees the flag and the
    # path together. `_logical_lines()` exists for precisely this reason and this
    # fence was not using it.
    for name, lines in _probe_path_logical_lines().items():
        for line in lines:
            if FIELD_FLAGS.search(line) and PROTECTION_PATH.search(line):
                offenders.append(f"{name}: {line[:120]}")
    assert not offenders, (
        "a `gh api` field flag co-occurs with a protection path, which makes the "
        f"call an implicit POST: {offenders}"
    )


def test_f11_the_matcher_itself_catches_the_bypass_it_names():
    """⚠ A matcher fence that never sees a positive is indistinguishable from a
    broken regex. Prove both patterns fire on the real bypass string before
    trusting either green above."""
    bypass = 'gh api repos/o/r/branches/main/protection/enforce_admins -f _=""'
    assert FIELD_FLAGS.search(bypass) and PROTECTION_PATH.search(bypass)
    assert not METHOD_FLAGS.search(bypass), "the bypass has no -X, which is the point"
    # ⚠ Every spelling `gh`/`curl` actually accept. `-XDELETE` with no separator
    # and a lowercase verb both slipped past the original matcher.
    for spelling in ('gh api --method DELETE repos/o/r/x',
                     'curl -X PUT https://api.github.com/x',
                     'gh api -XDELETE repos/o/r/x',
                     'gh api -X delete repos/o/r/x',
                     'gh api --method=post repos/o/r/x',
                     'curl -XPATCH https://api.github.com/x'):
        assert METHOD_FLAGS.search(spelling), f"matcher missed: {spelling}"
    for benign in ('gh api repos/o/r/branches/main --jq .protected',
                   '# never -X PUT against protection'):
        pass
    # And the continuation join must bring flag and path together.
    joined = _probe_path_logical_lines()
    assert any("branches/main/protection" in ln and "api" in ln
               for ln in joined[PROBE]), (
        "the probe's protection fetch did not survive the continuation join")


# ---------------------------------------------------------------------------
# The `${GH:-gh}` stub. What makes every probe path drivable with no credential.
# ---------------------------------------------------------------------------
def _fake_gh(tmp_path, *, protection_body, branch_body, rules_body,
             app_token_valid=True) -> pathlib.Path:
    """A `gh` stand-in, driven entirely by files.

    ⚠ IT IS CREDENTIAL-AWARE, and that is what makes F9c possible at all. The
    real failure being modelled is a suspended/revoked App: the ADMIN-scoped call
    dies while the public-metadata call still works. A stub that answers every
    call identically cannot tell a one-credential probe from a two-credential
    one, and F9c would pass against the very design it exists to kill.
    """
    state = tmp_path / "gh-state"
    state.mkdir(exist_ok=True)
    (state / "answers.json").write_text(json.dumps({
        "protection": protection_body, "branch": branch_body,
        "rules": rules_body, "app_ok": app_token_valid,
    }))
    gh = tmp_path / "fake-gh"
    gh.write_text(f'''#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path({str(state)!r})
a = json.loads((state / "answers.json").read_text())
args = sys.argv[1:]
with (state / "argv.log").open("a") as fh:
    fh.write(json.dumps({{"argv": args, "token": os.environ.get("GH_TOKEN", "")}}) + "\\n")
target = " ".join(args)
token = os.environ.get("GH_TOKEN", "")
if "/protection" in target:
    # Only the App token can read protection. A revoked App yields nothing.
    if token != "app-token" or not a["app_ok"]:
        sys.exit(1)
    out = a["protection"]
    # ⚠⚠ THE STUB HONOURS --jq, and it must. A stub that ignores the flag makes
    # F20's behavioural half VACUOUS: the canary survives a projected fetch
    # because the stub never projected. Measured -- a projection mutant survived
    # the entire suite until this was implemented.
    for i, x in enumerate(args):
        if x in ("--jq", "-q") and i + 1 < len(args):
            expr = args[i + 1]
            keys = [k.strip() for k in expr.strip("{{}} ").split(",") if k.strip()]
            try:
                doc = json.loads(out)
            except ValueError:
                doc = {{}}
            out = json.dumps({{k: doc[k] for k in keys if k in doc}})
    sys.stdout.write(out)
elif "/rules/branches" in target:
    sys.stdout.write(a["rules"])
else:
    body = json.loads(a["branch"]) if a["branch"].strip() else {{}}
    # ⚠⚠ NESTED PATHS, not a single key. With a one-level lookup a probe reading
    # `.protection.required_status_checks.enforcement_level` got None -> unknown
    # -> `in-posture` on a good payload, so the fence that exists to kill exactly
    # that mutant COULD NOT EXPRESS IT. Same class as the stub that ignored
    # `--jq` and made the F20 canary vacuous.
    for x in args:
        if x.startswith("."):
            for part in [p for p in x.split(".") if p]:
                body = body.get(part) if isinstance(body, dict) else None
    if isinstance(body, bool):
        sys.stdout.write("true" if body else "false")
    elif body is None:
        sys.stdout.write("")
    else:
        sys.stdout.write(json.dumps(body))
''')
    gh.chmod(0o755)
    return gh


def _run_probe(gh: pathlib.Path, tmp_path, posture_path=None,
               admin_token="app-token"):
    return subprocess.run(
        ["bash", str(_artifact(PROBE))],
        capture_output=True, text=True,
        env={**os.environ, "GH": str(gh), "REPO": "flamarion/tbd",
             "POSTURE_FILE": str(posture_path or _artifact(POSTURE)),
             "PROBE_ADMIN_TOKEN": admin_token,
             "PROBE_METADATA_TOKEN": "metadata-token",
             "GITHUB_OUTPUT": ""},
    )


def _probe_verdict(result) -> str:
    """The probe prints both readings; the machine-readable answer is the final
    `verdict=` line. Reading line 1 would read a banner."""
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("verdict=")]
    return lines[-1].split("=", 1)[1].strip() if lines else ""


def _gh_calls(tmp_path) -> list[dict]:
    log = tmp_path / "gh-state" / "argv.log"
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def _branch_payload(protected=True):
    return json.dumps({"name": "main", "protected": protected})


# ---------------------------------------------------------------------------
# F20. THE fetch-layer projection fence. Highest value in the table.
# ---------------------------------------------------------------------------
def _logical_lines(relpath: str) -> list[str]:
    """Executable lines with shell line-continuations JOINED.

    ⚠⚠ MEASURED: a projection mutant that put `--jq '{enforce_admins, ...}'` on a
    continuation line SURVIVED the whole suite. A per-physical-line matcher
    attributes the flag to a different "line" than the one carrying the URL, so
    the fence sees a clean fetch and a clean flag and objects to neither. Real
    `gh api` invocations in this repo are wrapped exactly that way.
    """
    joined, buf = [], ""
    for line in _executable_lines(relpath):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        joined.append((buf + stripped).strip())
        buf = ""
    if buf:
        joined.append(buf.strip())
    return joined


def test_f20_the_line_joiner_actually_joins():
    """Non-vacuity of the joiner the fence below depends on -- it was added in
    response to a survived mutant, which is exactly when an over-correction or a
    no-op would go unnoticed."""
    joined = _logical_lines(PROBE)
    fetch = [ln for ln in joined if "branches/main/protection" in ln]
    assert fetch, "the joiner produced no line carrying the protection fetch"
    assert any("GH_TOKEN" in ln and "api" in ln for ln in fetch), (
        f"the joined fetch line lost its command context: {fetch}")


def test_f20_the_protection_fetch_carries_no_projection():
    """⚠⚠ Every other comparison fence drives the checker from STDIN, so the F1
    defect is fully reintroducible one layer UP with all of them green:

        gh api repos/O/R/branches/main/protection --jq '{enforce_admins, ...}'

    plus a posture seeded from that same command compares four keys and reports
    `in-posture` forever. That is exactly the four-field projection that hid
    `allow_force_pushes` for three weeks, moved into the fetch."""
    offenders = []
    for line in _logical_lines(PROBE):
        # ⚠ The `.protected` fetch legitimately uses `--jq`: it is a one-bit
        # oracle, not a posture source. Exempt it by requiring the PROTECTION
        # path specifically.
        if not PROTECTION_PATH.search(line):
            continue
        for flag in ("--jq", " -q ", "--template", "--jq=", "-q=", "| jq"):
            if flag in line:
                offenders.append(f"{line[:130]} (carries {flag.strip()})")
    assert not offenders, (
        "the protection fetch must retrieve the WHOLE document; a server-side "
        f"projection is invisible to every stdin-driven fence: {offenders}"
    )


def test_f20_a_canary_key_survives_the_probes_pipeline_into_the_checker(tmp_path):
    """The behavioural half, and the one a reviewer should trust. Structural
    flag-matching dies to `--jq` spelled any new way, or to a `jq` pipe added
    downstream. Inject a key nobody has ever seen into the stubbed response: if
    the probe's pipeline preserves the document, the checker sees it and says
    `drifted` naming it. If anything projects, the verdict is `in-posture`."""
    live = _raw_live()
    # ⚠ THE TWO HALVES OF F20 ARE NOT SYMMETRIC, so neither may be dropped. The
    # stub models `--jq` object-construction, so a `--jq` projection dies to both
    # halves -- but a `--template` projection is caught ONLY by the structural
    # half, because the stub returns the whole document for it.
    live["canary_key_no_projection_may_drop"] = {"enabled": True}
    gh = _fake_gh(tmp_path, protection_body=json.dumps(live),
                  branch_body=_branch_payload(), rules_body="[]")
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "drifted", (
        "the canary did not reach the comparison -- something between the fetch "
        f"and stdin is projecting.\n{r.stdout}{r.stderr}"
    )
    assert "canary_key_no_projection_may_drop" in r.stdout, r.stdout


def test_f20_the_canary_test_can_actually_fail(tmp_path):
    """⚠ Non-vacuity of F20 itself. If the stub or the pipeline were broken in a
    way that made EVERY payload read as drifted, the fence above would pass for
    the wrong reason. The same stub without a canary must be `in-posture`."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(), rules_body="[]")
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"


def test_f20_the_probe_asks_for_the_protection_document_itself():
    """A projection can also be achieved by asking a SUB-RESOURCE
    (`/protection/enforce_admins`) instead of `/protection`. Same blindness,
    no `--jq` in sight."""
    argv_lines = [ln for ln in _executable_lines(PROBE) if PROTECTION_PATH.search(ln)]
    assert argv_lines, "the probe never fetches a protection path at all"
    assert any(re.search(r"branches/[^\s\"']*/protection[\"'\s]", ln + " ")
               for ln in argv_lines), (
        f"the probe fetches only protection SUB-resources: {argv_lines}")


# ---------------------------------------------------------------------------
# F8 (probe half). The disambiguator stays one bit.
# ---------------------------------------------------------------------------
def test_the_disambiguator_never_consumes_dot_protection():
    """⚠ `.protected` is NOT a classic-protection bit -- it reads `true` under a
    ruleset too. It is a one-bit "is anything protecting this branch" oracle. The
    same endpoint also exposes
    `protection.required_status_checks.enforcement_level`, which IS
    `enforce_admins`; reading it would turn a disambiguator into a partial source
    of posture, buying one field and selling the rest."""
    offenders = []
    for rel in (CHECK, PROBE):
        for line in _executable_lines(rel):
            if "enforcement_level" in line or ".protection." in line:
                offenders.append(f"{rel}: {line.strip()[:90]}")
    assert not offenders, f"the `.protected` read must stay ONE BIT: {offenders}"


def test_the_checker_reads_exactly_three_env_inputs():
    """⚠ `PROTECTION_HTTP_STATUS` was SUBTRACTED in revision 2. A status code
    could not have carried the disambiguation anyway: 404 is what an unprotected
    branch returns AND what an under-permissioned token returns, and for a GitHub
    App a missing permission commonly surfaces as 403 instead."""
    text = "\n".join(_executable_lines(CHECK))
    seen = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', text))
    assert seen == {"BRANCH_PROTECTED", "EFFECTIVE_RULES_JSON", "POSTURE_FILE"}, seen


# ---------------------------------------------------------------------------
# F9c. THE credential split. Step 1 is worthless without it.
# ---------------------------------------------------------------------------
def test_f9c_the_two_reads_use_different_credentials(tmp_path):
    """⚠⚠ Moving `.protected` to step 1 BUYS NOTHING if both reads share one
    credential: a suspended App fails every call, `BRANCH_PROTECTED` comes back
    unknown, and a genuinely naked `main` reports `could-not-run` -- the exact
    fail-open the reorder was performed to prevent.

    F9 drives the checker from an env var and structurally cannot see this."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(), rules_body="[]")
    _run_probe(gh, tmp_path)
    calls = _gh_calls(tmp_path)
    assert calls, "the probe made no gh calls at all"
    by_token = {}
    for c in calls:
        target = " ".join(c["argv"])
        kind = "protection" if "/protection" in target else (
            "rules" if "/rules/branches" in target else "metadata")
        by_token.setdefault(kind, set()).add(c["token"])
    assert by_token.get("protection") == {"app-token"}, (
        f"the /protection read must use the App token: {by_token}")
    assert by_token.get("metadata") == {"metadata-token"}, (
        f"the .protected read must use the workflow token, which needs only "
        f"`contents: read` and survives an App outage: {by_token}")
    assert by_token["protection"] != by_token["metadata"], by_token


def test_f9c_a_failed_app_auth_still_reports_a_naked_main_as_drifted(tmp_path):
    """THE behavioural fence. The App is revoked, so `/protection` returns
    nothing -- and `main` is genuinely naked. A single-credential probe reports
    `could-not-run` here. The split reports `drifted`, which is the whole point
    of putting `.protected` first."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=False), rules_body="[]",
                  app_token_valid=False)
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "drifted", (
        "a revoked App masked a naked main -- the two reads are sharing a "
        f"credential.\n{r.stdout}{r.stderr}")
    assert r.returncode == 1


def test_f9c_a_failed_app_auth_on_a_protected_main_is_could_not_run(tmp_path):
    """The inverse, so the fence above cannot be satisfied by "always drifted"."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=True), rules_body="[]",
                  app_token_valid=False)
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "could-not-run", f"{r.stdout}{r.stderr}"
    assert r.returncode == 2


def test_f9c_the_notifier_does_not_use_the_admin_token():
    """⚠ The App holds `Administration: read` and CANNOT open issues. Pointing
    `GH_TOKEN` at it for the whole job makes the notifier 403 and exit 1: drift
    detected, no issue opened, a red square nobody opens."""
    steps = {s.get("id"): s for s in _doc()["jobs"]["probe"]["steps"] if s.get("id")}
    alarm_env = steps["alarm"].get("env", {})
    token = str(alarm_env.get("GH_TOKEN", ""))
    assert "github.token" in token, (
        f"the notifier step's GH_TOKEN is {token!r}; it must be the workflow "
        "token, because the App cannot write issues")
    assert "auth.outputs.token" not in token, token


# ---------------------------------------------------------------------------
# F9d. The fail-open ONE LAYER UP, in the workflow's own shell.
# ---------------------------------------------------------------------------
def _parse_github_output(text: str) -> dict[str, str]:
    """Parse `$GITHUB_OUTPUT` the way Actions does, honouring `key<<DELIM` blocks.

    ⚠⚠ A NAIVE LINE SCAN READS THE WRONG VALUE, AND IT COST THIS FENCE ITS
    TEETH. The step writes `detail<<DETAIL_EOF` whose body is the probe's own
    stdout -- which itself contains a line `verdict=drifted`. So a scan for the
    last `verdict=` line picks that one out of the LOG BLOB, not the step output.
    Measured: with the auth-override guard deliberately removed, the step
    correctly emitted `verdict=could-not-run` and this fence still read
    `drifted` and passed. The mutant it exists to kill SURVIVED.

    Real Actions is not fooled -- the heredoc body is just the value of `detail`
    -- so this is a defect in the fence, not in the workflow. But a fence that
    reads a different value than the runner does is not fencing the runner.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and not line.startswith(" "):
            key, delim = line.split("<<", 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key.strip()] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value
        i += 1
    return out


def test_the_github_output_parser_is_not_fooled_by_the_detail_heredoc():
    """Non-vacuity of the parser above, pinned against the exact shape that
    defeated it: a `verdict=` line living INSIDE the `detail` heredoc body."""
    sample = (
        "verdict=could-not-run\n"
        "detail<<DETAIL_EOF\n"
        "could not mint an installation token\n"
        "drifted\n"
        "verdict=drifted\n"
        "DETAIL_EOF\n"
    )
    parsed = _parse_github_output(sample)
    assert parsed["verdict"] == "could-not-run", parsed
    assert "verdict=drifted" in parsed["detail"]


def _check_step() -> dict:
    steps = {st.get("id"): st for st in _steps() if st.get("id")}
    assert "check" in steps, f"no step with id 'check'; ids were {sorted(steps)}"
    return steps["check"]


def _run_the_check_step(tmp_path, gh, *, auth_outcome, app_token):
    """EXECUTE the workflow step's own `run:` block.

    ⚠⚠ F9c proves the PROBE SCRIPT keeps its two credentials apart. It cannot
    see this: the workflow re-derives the verdict in shell afterwards, and an
    auth-outcome override there can overwrite a legitimate `drifted` -- earned
    via the metadata token on a naked `main` -- with `could-not-run`, with the
    probe script perfectly correct and every F9c fence green. That is the F20
    "one layer up" class, and a structural grep over the YAML would only be
    another string match. So the block is parsed out and RUN.
    """
    step = _check_step()
    script = str(step["run"]).replace("${{ steps.auth.outcome }}", auth_outcome)

    # ⚠⚠ RESOLVE THE PROBE PATH, and assert the substitution landed. The block
    # invokes `bash scripts/ci/probe-branch-protection.sh` relative to the repo
    # root; inside the backend container that path is backend/scripts and the
    # file is not there. MEASURED: the script then silently did nothing, the
    # verdict came back EMPTY, the auth-override turned it into `could-not-run`,
    # and `test_f9d_a_revoked_app_on_a_protected_main_still_reports_could_not_run`
    # PASSED WITHOUT EVER RUNNING THE PROBE. A fence that is satisfied by its
    # subject being absent is worse than no fence.
    rel_probe = "scripts/ci/probe-branch-protection.sh"
    assert rel_probe in script, (
        f"the check step no longer invokes {rel_probe}; this fence would be "
        f"running a script that never calls the probe: {script}")
    script = script.replace("bash " + rel_probe, "bash " + str(_artifact(PROBE)))
    assert f"bash {rel_probe}" not in script, script
    assert str(_artifact(PROBE)) in script

    assert "${{" not in script, (
        f"the check step's run: block carries an unsubstituted expression, so "
        f"this fence would be testing a different script than CI runs: {script}")

    subs = {
        "${{ steps.auth.outputs.token }}": app_token,
        "${{ github.token }}": "metadata-token",
        "${{ github.repository }}": "flamarion/tbd",
    }
    env = {}
    for key, raw in (step.get("env") or {}).items():
        value = str(raw)
        for needle, replacement in subs.items():
            value = value.replace(needle, replacement)
        assert "${{" not in value, f"unsubstituted expression in env {key}: {value}"
        env[key] = value

    out = tmp_path / "step-output"
    out.write_text("")
    script_file = tmp_path / "check-step.sh"
    script_file.write_text(script)
    r = subprocess.run(
        ["bash", str(script_file)], cwd=str(REPO_ROOT), capture_output=True,
        text=True, env={**os.environ, **env, "GH": str(gh),
                        "GITHUB_OUTPUT": str(out)})
    # ⚠⚠ THE GUARD LIVES IN THE HARNESS, NOT IN ONE FENCE. In the backend
    # container this block ran with an unresolvable probe path. The vacuity
    # review predicted `..._on_a_protected_main_still_reports_could_not_run`
    # would then pass FOR THE WRONG REASON -- probe missing -> empty verdict ->
    # auth override -> `could-not-run` -> assertion matches. It actually FAILED,
    # because C4 got there first: under `set -e` + `pipefail` the no-match `grep`
    # killed the step before ANY output was written, so no verdict was set at all.
    #
    # ⚠ The reviewer's mechanism was therefore real and merely MASKED by a second
    # defect -- and fixing C4 (`|| true`) UNMASKS it. Asserting the stub was
    # actually called makes every F9d fence structurally incapable of passing
    # with the probe absent, on any input, instead of relying on one of them
    # happening to notice.
    assert _gh_calls(tmp_path), (
        "the workflow shell made no gh calls, so the probe never ran. Every "
        "assertion about the verdict would be describing an absent script rather "
        f"than this design.\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    return _parse_github_output(out.read_text()).get("verdict", ""), r


def test_f9d_the_harness_actually_executes_the_probe(tmp_path):
    """⚠⚠ Non-vacuity of F9d itself, and it is not theoretical: in the backend
    container this harness ran the workflow shell with a probe path that did not
    resolve, so the probe never ran, the verdict was empty, and three of the four
    F9d fences passed anyway -- one of them by asserting the very value an absent
    probe produces. Prove the stub was actually called."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=True), rules_body="[]")
    verdict, r = _run_the_check_step(tmp_path, gh, auth_outcome="success",
                                     app_token="app-token")
    calls = _gh_calls(tmp_path)
    assert len(calls) == 3, (
        f"the workflow shell made {len(calls)} gh calls; the probe cannot have "
        f"run.\n{r.stdout}{r.stderr}")
    assert verdict == "in-posture", verdict


def test_f9d_a_revoked_app_cannot_downgrade_a_naked_mains_drifted_verdict(tmp_path):
    """⚠⚠ THE fence for the one place this design was still exposed. The App is
    revoked AND `main` is genuinely naked. The metadata token still reads
    `.protected == false`, so the probe correctly says `drifted`. The workflow's
    auth-outcome branch must NOT overwrite that with `could-not-run`.

    Drop `&& "$verdict" != "drifted"` from the guard and this is the exact
    fail-open that putting `.protected` first exists to prevent -- reintroduced
    in shell, in the workflow, after the probe has already got it right."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=False), rules_body="[]",
                  app_token_valid=False)
    verdict, r = _run_the_check_step(tmp_path, gh, auth_outcome="failure",
                                     app_token="")
    assert verdict == "drifted", (
        "the workflow downgraded a confirmed naked-main `drifted` to "
        f"{verdict!r} because the App auth failed.\n{r.stdout}{r.stderr}")


def test_f9d_a_revoked_app_on_a_protected_main_still_reports_could_not_run(tmp_path):
    """The inverse, so the fence above cannot be satisfied by "always drifted".
    Here the App failure genuinely IS the reason we cannot answer, and the
    workflow must say so -- `continue-on-error` on the auth step only helps if
    something downstream converts the failed outcome into a verdict."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=True), rules_body="[]",
                  app_token_valid=False)
    verdict, r = _run_the_check_step(tmp_path, gh, auth_outcome="failure",
                                     app_token="")
    assert verdict == "could-not-run", f"{verdict!r}\n{r.stdout}{r.stderr}"
    assert "installation token" in r.stdout.lower() or "App" in r.stdout


def test_f9d_a_healthy_run_reports_in_posture_through_the_workflow_shell(tmp_path):
    """Non-vacuity of the harness: if the extracted block could not run at all,
    both fences above would pass for the wrong reason on an empty verdict."""
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(protected=True), rules_body="[]")
    verdict, r = _run_the_check_step(tmp_path, gh, auth_outcome="success",
                                     app_token="app-token")
    assert verdict == "in-posture", f"{verdict!r}\n{r.stdout}{r.stderr}"


def test_f9d_the_workflow_shell_reports_real_drift_through_the_whole_chain(tmp_path):
    """And the chain carries a genuine drift end to end, with auth healthy."""
    drifted = _raw_live()
    drifted["enforce_admins"] = {"enabled": False}
    gh = _fake_gh(tmp_path, protection_body=json.dumps(drifted),
                  branch_body=_branch_payload(protected=True), rules_body="[]")
    verdict, r = _run_the_check_step(tmp_path, gh, auth_outcome="success",
                                     app_token="app-token")
    assert verdict == "drifted", f"{verdict!r}\n{r.stdout}{r.stderr}"
    assert "enforce_admins" in r.stdout


# ---------------------------------------------------------------------------
# Read ONCE. The re-read is subtracted.
# ---------------------------------------------------------------------------
def test_the_probe_reads_once_and_reports_once(tmp_path):
    """⚠⚠ THE RE-READ IS SUBTRACTED. Its severity table's first row
    (`drifted -> in-posture` ⇒ `in-posture`) encoded *"observed drift, zero
    alarms"* -- the exact pattern this design calls fatal one page earlier to
    justify `cancel-in-progress: false` -- and its fence F13 FENCED THE DEFECT
    IN, so a future agent who fixed it would go red and revert.

    It also made the probe blind by construction to the one event it is uniquely
    placed to see: a break-glass disarm has no other automated trace in this
    repo. A rare stale alarm is absorbed by the title-prefix dedupe."""
    drifted = _raw_live()
    drifted["enforce_admins"] = {"enabled": False}
    gh = _fake_gh(tmp_path, protection_body=json.dumps(drifted),
                  branch_body=_branch_payload(), rules_body="[]")
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "drifted", f"{r.stdout}{r.stderr}"
    assert len(_gh_calls(tmp_path)) == 3, (
        "a non-healthy verdict triggered more than one reading -- the re-read is "
        f"subtracted: {_gh_calls(tmp_path)}")
    emitted = [ln for ln in r.stdout.splitlines() if ln.startswith("verdict=")]
    assert emitted == ["verdict=drifted"], emitted


def test_w10_the_probe_refuses_to_run_without_an_explicit_repo(tmp_path):
    """⚠ The probe's header argues that a hardcoded fallback would report
    `in-posture` about the WRONG repository. That argument was comment-only --
    exactly the state F7 was upgraded out of, in a repo whose standing rule is
    that a grep is satisfied by a comment. `${REPO:?}` must actually abort.

    ⚠ This fence was written once and LOST: the batch that added it aborted on an
    earlier failing edit, and only the surviving `W10-repo-default-restored`
    mutant revealed that the fence had never landed. A mutant campaign is the
    only thing that distinguishes "fence present" from "fence believed present".
    """
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(), rules_body="[]")
    env = {**os.environ, "GH": str(gh),
           "POSTURE_FILE": str(_artifact(POSTURE)),
           "PROBE_ADMIN_TOKEN": "app-token",
           "PROBE_METADATA_TOKEN": "metadata-token", "GITHUB_OUTPUT": ""}
    env.pop("REPO", None)
    r = subprocess.run(["bash", str(_artifact(PROBE))], capture_output=True,
                       text=True, env=env)
    assert r.returncode != 0, (
        f"the probe ran with REPO unset and exited {r.returncode}; it must abort "
        f"rather than guess a repository.\n{r.stdout}")
    assert "REPO" in r.stderr, r.stderr
    assert not _gh_calls(tmp_path), "it contacted GitHub before noticing REPO was unset"
    body = "\n".join(_executable_lines(PROBE))
    assert "flamarion/tbd" not in body, (
        "the probe hardcodes a repository; it must come from the workflow, or a "
        "run anywhere else reports `in-posture` about a branch nobody asked about")


def test_c10_an_unexpected_crash_is_could_not_run_never_drifted():
    """⚠⚠ AN UNCAUGHT PYTHON EXCEPTION EXITS 1, AND THIS SCRIPT'S OWN CONTRACT
    DEFINES 1 AS `drifted`. So any shape nobody anticipated would be reported as
    a CONFIRMED floor change and open an incident naming a drift that did not
    happen -- and the operator's trained response to a false drift is to loosen
    the probe. A crash confirms nothing, which is exactly `could-not-run`.

    Driven with a payload deep enough to exhaust the recursion limit inside the
    normalizer's tree walk -- a real crash, not a mocked one."""
    inner = {"enabled": True}
    for _ in range(5000):
        inner = {"nest": inner}
    r = _run_check({"enforce_admins": inner})
    assert r.returncode == 2, (
        f"a crashing checker exited {r.returncode}. Exit 1 means `drifted`, so a "
        f"crash would be reported as a confirmed floor change.\n{r.stdout}{r.stderr}")
    assert _verdict(r) == "could-not-run", r.stdout
    assert "crashed" in r.stdout


def test_the_probe_never_sleeps():
    """The re-read's `sleep` must be gone, not merely defaulted to zero: a
    default is a knob, and a knob invites the mechanism back."""
    body = "\n".join(_executable_lines(PROBE))
    assert "sleep" not in body, "the probe still sleeps; the re-read is subtracted"


def test_a_healthy_reading_also_costs_exactly_one_read(tmp_path):
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(), rules_body="[]")
    r = _run_probe(gh, tmp_path)
    assert _probe_verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"
    assert len(_gh_calls(tmp_path)) == 3


def test_c5_only_the_workflow_writes_the_verdict_output(tmp_path):
    """⚠⚠ ONE WRITER. The probe used to append `verdict=` to `$GITHUB_OUTPUT`
    itself -- and because it runs inside the check step's command substitution,
    it and the workflow appended to the SAME FILE. Correctness rested entirely on
    runner last-wins, with BOTH the auth override and the empty-verdict fallback
    silently staked on it. The contract is now one-way: the probe emits
    `verdict=<token>` as the last line of stdout and writes no file; the workflow
    is the sole writer of the step output."""
    out = tmp_path / "ghout"
    out.write_text("")
    gh = _fake_gh(tmp_path, protection_body=json.dumps(_raw_live()),
                  branch_body=_branch_payload(), rules_body="[]")
    r = subprocess.run(
        ["bash", str(_artifact(PROBE))], capture_output=True, text=True,
        env={**os.environ, "GH": str(gh), "REPO": "flamarion/tbd",
             "POSTURE_FILE": str(_artifact(POSTURE)),
             "PROBE_ADMIN_TOKEN": "app-token",
             "PROBE_METADATA_TOKEN": "metadata-token",
             "GITHUB_OUTPUT": str(out)})
    assert out.read_text() == "", (
        "the probe wrote to $GITHUB_OUTPUT. Two writers append to one file and "
        f"the result depends on runner last-wins: {out.read_text()!r}")
    assert r.stdout.strip().splitlines()[-1] == "verdict=in-posture", r.stdout
    body = "\n".join(_executable_lines(PROBE))
    assert "GITHUB_OUTPUT" not in body, (
        "the probe references GITHUB_OUTPUT; the workflow owns that file")


def test_c4_a_probe_emitting_no_verdict_line_still_alarms(tmp_path):
    """⚠⚠ `set -e` + `pipefail` made the `${verdict:-could-not-run}` fallback
    UNREACHABLE. A no-match `grep` fails the assignment, which kills the step
    before any output is written -- and with NO outputs set both the alarm and
    the fail step carry an implicit `success() &&` and are SKIPPED. Red square,
    no issue, no alarm: exactly what `continue-on-error` on the auth step exists
    to prevent, reintroduced three lines later.

    Measured before the fix: probe emits no `verdict=` line -> step exit 1, zero
    outputs. This drives that case through the real step body."""
    step = _check_step()
    script = str(step["run"]).replace("${{ steps.auth.outcome }}", "success")
    silent = tmp_path / "silent-probe.sh"
    silent.write_text("#!/usr/bin/env bash\necho 'this probe prints no verdict line'\n")
    silent.chmod(0o755)
    script = script.replace("bash scripts/ci/probe-branch-protection.sh",
                            f"bash {silent}")
    assert "${{" not in script and str(silent) in script
    out = tmp_path / "ghout"
    out.write_text("")
    path = tmp_path / "step.sh"
    path.write_text(script)
    r = subprocess.run(["bash", str(path)], cwd=str(REPO_ROOT), capture_output=True,
                       text=True, env={**os.environ, "GITHUB_OUTPUT": str(out)})
    parsed = _parse_github_output(out.read_text())
    assert "verdict" in parsed, (
        "the step set NO outputs, so the alarm and fail steps would both be "
        f"skipped and the drift would be silent. exit={r.returncode}\n"
        f"{r.stdout}{r.stderr}")
    assert parsed["verdict"] == "could-not-run", parsed
    assert parsed["verdict"] != "in-posture"


def test_c4_a_probe_that_dies_entirely_still_alarms(tmp_path):
    """The harsher half: the script is missing outright."""
    step = _check_step()
    script = str(step["run"]).replace("${{ steps.auth.outcome }}", "success")
    script = script.replace("bash scripts/ci/probe-branch-protection.sh",
                            "bash /nonexistent/probe.sh")
    out = tmp_path / "ghout"
    out.write_text("")
    path = tmp_path / "step.sh"
    path.write_text(script)
    subprocess.run(["bash", str(path)], cwd=str(REPO_ROOT), capture_output=True,
                   text=True, env={**os.environ, "GITHUB_OUTPUT": str(out)})
    parsed = _parse_github_output(out.read_text())
    assert parsed.get("verdict") == "could-not-run", (
        f"a dead probe must still alarm; outputs were {parsed}")


# ---------------------------------------------------------------------------
# F16. Kills: a self-regenerating posture -- while ALLOWING a stdout generator.
# ---------------------------------------------------------------------------
def test_f16_nothing_in_the_repo_writes_the_posture_file():
    """⚠ Nothing may regenerate this file IN PLACE. The diff hunk IS the evidence
    a human looked -- the posture `report-sources.json` and `.test_durations`
    already pay for.

    ⚠⚠ POSITIVE ANCHOR, and it is not decoration. MEASURED in revision 1: with
    the whole implementation deleted this fence PASSED. A search fence with no
    anchor cannot tell "nothing writes the file" from "nothing was searched", and
    every silent death of it (a rename, a moved directory, a glob typo) looks
    like the second while reading as the first."""
    root = REPO_ROOT
    assert root is not None
    # ⚠ A NAMED corpus, each part asserted non-empty. A bare recursive glob
    # cannot distinguish "nothing writes the file" from "the glob matched
    # nothing", and MEASURED in revision 1 this fence passed with the entire
    # implementation deleted.
    #
    # ⚠ The corpus is WIDER than the three new files: this fence's name is
    # "nothing in the repo writes the posture file", so anything that could
    # plausibly acquire that behaviour must be in it.
    sd = _scripts_dir()
    # ⚠ backend/scripts is `<root>/backend/scripts` on a checkout but `/app/scripts`
    # inside the container -- the mirror image of the repo-root case, and the
    # reason `_scripts_dir()` exists. Resolving either one against REPO_ROOT
    # alone is wrong in exactly one of the two environments.
    backend_scripts = next(
        (d for d in (root / "backend" / "scripts", root / "scripts")
         if (d / "migrate.py").is_file()), None)
    assert backend_scripts is not None, "could not locate backend/scripts"
    parts = {
        "scripts/ci/*": sorted(sd.glob("ci/*")),
        "scripts/*.sh": sorted(sd.glob("*.sh")),
        "backend/scripts/*.py": sorted(backend_scripts.glob("*.py")),
        "workflows": sorted((root / ".github" / "workflows").glob("*.yml")),
    }
    for label, found in parts.items():
        assert found, f"the {label} corpus is EMPTY, so this fence searched nothing"
    # ⚠ `pfv` is NOT mounted into the backend container, so it is OPTIONAL rather
    # than asserted -- an unconditional assert here is a false RED in the one
    # environment the documented local command has to work in, and a false red is
    # what gets a fence weakened rather than obeyed.
    parts["pfv (optional)"] = [p for p in [root / "pfv"] if p.is_file()]
    for required in ("check-branch-protection.sh", "probe-branch-protection.sh",
                     "normalize_protection.py", "notify-protection-drift.sh",
                     "branch-protection-probe.yml",
                     # ⚠ THIS PR EDITS breakglass-merge.sh, it already POSTs to
                     # the protection endpoint, and "the re-arm failed, so
                     # regenerate the posture" is its single most natural home.
                     # Excluding it made the fence's name a lie.
                     "breakglass-merge.sh"):
        names = {p.name for group in parts.values() for p in group}
        assert required in names, (
            f"{required} was not in the scanned corpus, so this fence searched "
            "the wrong tree and its green means nothing.")
    corpus = [p for group in parts.values() for p in group if p.is_file()]
    names = {p.name for p in corpus}
    for required in ("check-branch-protection.sh", "probe-branch-protection.sh",
                     "normalize_protection.py",
                     "notify-protection-drift.sh", "branch-protection-probe.yml"):
        assert required in names, (
            f"{required} was not in the scanned corpus, so this fence searched "
            "the wrong tree and its green means nothing.")
    # ⚠⚠ THE MATCH IS ANCHORED ON THE WRITE TARGET, not on "the token appears
    # near a `>`". MEASURED: the loose form flagged three innocent lines at once
    # -- `--label branch-protection >/dev/null`, a filename
    # (`probe-branch-protection.sh 2>&1`), and `echo "...verdict..." >&2`. A
    # fence with a false-positive rate like that gets deleted, not obeyed.
    offenders = []
    for path in corpus:
        for s in _code_lines(path):
            if WRITES_POSTURE.search(s):
                offenders.append(f"{path.relative_to(root)}: {s[:90]}")
    assert not offenders, f"something writes the posture file in place: {offenders}"


def test_f16_the_regenerator_matcher_sees_every_spelling_it_claims_to():
    """⚠ Non-vacuity of the half above, pinned against the exact spellings that
    escaped it. This matcher has now been widened three times after a mutant got
    through, which is precisely when an over-correction or a silent no-op goes
    unnoticed."""
    def opens_for_write(b):
        return any(m.group(1)[0] in "wax" for m in
                   re.finditer(r'open\s*\([^)]*?,\s*["\']([a-z+]{1,3})["\']', b))
    assert opens_for_write('open(posture_path, "w", encoding="utf-8")')
    assert opens_for_write("open(p, 'a')")
    assert not opens_for_write('open(posture_path, "r", encoding="utf-8")')

    def pathlib_write(b):
        return bool(re.search(r"\.write_text\s*\(|\.write_bytes\s*\(|shutil\.copy", b))
    assert pathlib_write('pathlib.Path(os.environ["POSTURE_FILE"]).write_text(doc)')
    assert not pathlib_write("doc = fh.read()")

    def shell_write(b):
        return bool(re.search(r'>\s*"?\$\{?(POSTURE_FILE|POSTURE)\b', b))
    assert shell_write("""printf '%s' "$doc" > "$POSTURE_FILE" """)
    assert shell_write("cat x >$POSTURE_FILE")
    assert not shell_write('echo "verdict=$verdict" >> "$GITHUB_OUTPUT"')


def test_f16_the_write_matcher_catches_the_writes_it_claims_to():
    """⚠ A matcher fence that never sees a positive is indistinguishable from a
    broken regex, and this one was tightened after a false-positive storm --
    exactly when an over-correction would go unnoticed."""
    for bad in (
        'gh api ... > .github/branch-protection/main.json',
        'gh api ... >>.github/branch-protection/main.json',
        'tee .github/branch-protection/main.json',
        'tee -a .github/branch-protection/main.json',
        'cp /tmp/new.json .github/branch-protection/main.json',
        'mv /tmp/new.json .github/branch-protection/main.json',
    ):
        assert WRITES_POSTURE.search(bad), f"matcher missed a real write: {bad}"
    for ok in (
        'gh issue create --label branch-protection >/dev/null 2>&1',
        'detail="$(bash scripts/ci/probe-branch-protection.sh 2>&1)"',
        'echo "branch-protection verdict: x" >&2',
        'POSTURE_FILE: .github/branch-protection/main.json',
    ):
        assert not WRITES_POSTURE.search(ok), f"matcher false-positives on: {ok}"


def test_f16_the_code_line_extractor_is_not_a_grep():
    """⚠ Non-vacuity of the extractor F16 depends on. MEASURED during the build:
    the generator's own usage docstring -- which documents the OPERATOR
    redirecting, the sanctioned path -- was flagged as a write by a
    comment-stripping grep. So the path scan has BOTH failure directions: it goes
    vacuous when nothing matches, and false-positive on documentation. Prose must
    be excluded by PARSING, not by a second grep heuristic, or the next author
    fixes the false red by deleting the check."""
    import textwrap
    sample = textwrap.dedent('''\
        """usage: prog > .github/branch-protection/main.json"""
        VALUE = 1
        ''')
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp()) / "f16_extractor_probe.py"
    tmp.write_text(sample)
    try:
        lines = _code_lines(tmp)
        assert not any("branch-protection" in ln for ln in lines), lines
        assert any("VALUE = 1" in ln for ln in lines), lines
    finally:
        tmp.unlink(missing_ok=True)


def test_f16_no_script_offers_an_in_place_regenerate_flag():
    """⚠ The half a PATH-grep cannot see, and the one that caught the real
    mutant: a regenerator resolving its path from an env var never spells
    `branch-protection` at all. MEASURED in revision 1 -- an
    `open(posture_path,"w")` mutant was invisible to the path scan above."""
    for rel in NEW_ARTIFACTS:
        # ⚠ `_code_lines`, not `_executable_lines`. The generator's own docstring
        # says "There is no --update, no --in-place" -- so a comment-stripping
        # grep is FOOLED BY THE COMMENT THAT EXPLAINS THE BAN. That is this
        # repo's standing hazard running in reverse, and it is why the fence
        # parses.
        body = "\n".join(_code_lines(_artifact(rel)))
        for flag in ("--update", "--write", "--regenerate", "--fix", "--in-place",
                     "--save", "--apply"):
            assert flag not in body, f"{rel} offers {flag}"
        # ⚠ MEASURED: the literal-suffix form (`', "w")' in body`) MISSED a real
        # regenerator mutant, because `open(p, "w", encoding="utf-8")` has a
        # comma after the mode, not a paren. Match the mode argument itself.
        # ⚠⚠ finditer, NOT search. MEASURED TWICE: the literal-suffix form
        # (`', "w")' in body`) missed the mutant because `open(p, "w",
        # encoding=...)` has a comma after the mode; and the first `re.search`
        # form ALSO missed it, because the checker legitimately opens the posture
        # with mode "r" EARLIER in the file, so the single match examined was
        # always the innocent one. A first-match matcher over a file that
        # contains a legitimate instance is structurally blind to every later
        # one.
        for m in re.finditer(r'open\s*\([^)]*?,\s*["\']([a-z+]{1,3})["\']', body):
            assert m.group(1)[0] not in "wax", (
                f"{rel} opens a file with mode {m.group(1)!r}; nothing may "
                "regenerate the posture in place")

        # ⚠⚠ TWO MORE SPELLINGS THAT WERE INVISIBLE TO BOTH HALVES, and they are
        # the natural ones for the env-var regenerator this half exists to catch:
        # neither spells `branch-protection`, and neither uses `open(`.
        #     pathlib.Path(os.environ["POSTURE_FILE"]).write_text(doc)
        #     printf '%s' "$doc" > "$POSTURE_FILE"
        assert not re.search(r"\.write_text\s*\(|\.write_bytes\s*\(|shutil\.copy",
                             body), (
            f"{rel} writes a file through pathlib/shutil; the posture may only "
            "be produced on stdout and redirected by the operator")
        assert not re.search(r'>\s*"?\$\{?(POSTURE_FILE|POSTURE)\b', body), (
            f"{rel} redirects into the posture path resolved from an env var -- "
            "invisible to a `branch-protection` path grep, which is the point")
        assert not re.search(r'\btee\s+(-a\s+)?"?\$\{?(POSTURE_FILE|POSTURE)\b',
                             body), rel


# ---------------------------------------------------------------------------
# F17a / F17b. The two halves of the normalizer's collapse.
# ---------------------------------------------------------------------------
def test_f17a_the_collapse_is_exact_key_set_not_membership():
    """⚠ A LOOSE collapse (`"enabled" in d`) returns the bool and SILENTLY
    DISCARDS every sibling key, reintroducing one level down exactly the
    projection blindness F2 exists to kill. It also reduces the envelope in
    EITHER order, which is why revision 1's single F17 passed against the very
    mutant it named."""
    out = _normalize_module().normalize(
        {"allow_force_pushes": {"enabled": True, "some_new_toggle": True}})
    assert out == {"allow_force_pushes": {"enabled": True, "some_new_toggle": True}}, (
        "a loose collapse dropped a sibling key -- a new GitHub toggle arriving "
        "inside an existing envelope would be invisible forever")


def test_f17b_a_url_bearing_enabled_envelope_normalizes_to_a_bool():
    """`enforce_admins` arrives as `{"url": ..., "enabled": true}` -- a TWO-key
    dict. Collapse before strip leaves it a dict and the posture grows a shape
    nobody intended. ⚠ Only meaningful GIVEN F17a."""
    out = _normalize_module().normalize(
        {"enforce_admins": {"url": f"{API}/enforce_admins", "enabled": True}})
    assert out == {"enforce_admins": True}, out
    assert isinstance(out["enforce_admins"], bool)


def test_f17_a_sibling_key_inside_an_envelope_reaches_the_comparison():
    live = _raw_live()
    live["allow_force_pushes"] = {"enabled": True, "some_new_toggle": True}
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f17_url_stripping_is_recursive_and_suffix_aware():
    mod = _normalize_module()
    assert mod.normalize({"a": {"contexts_url": "x", "contexts": ["c"]}}) == {
        "a": {"contexts": ["c"]}}
    assert mod.normalize({"urlencode": 1, "burl": 2}) == {"urlencode": 1, "burl": 2}


def test_f17_normalizing_the_raw_payload_reproduces_the_committed_posture():
    posture = json.loads(_artifact(POSTURE).read_text())
    assert _normalize_module().normalize(_raw_live()) == posture


# ---------------------------------------------------------------------------
# F18. Kills: an unsorted comparison that false-alarms on a GitHub reordering.
# ---------------------------------------------------------------------------
def test_f18_reordered_contexts_and_checks_are_not_drift():
    """⚠ `required_status_checks.checks` and `.contexts` have no guaranteed
    order, so an unsorted comparison alarms on a reordering with NOTHING changed
    -- a false `drifted` from the one probe whose entire value is credibility."""
    live = _raw_live()
    live["required_status_checks"]["contexts"].reverse()
    live["required_status_checks"]["checks"].reverse()
    r = _run_check(live)
    assert _verdict(r) == "in-posture", f"{r.stdout}{r.stderr}"


def test_f18_sorting_does_not_hide_a_changed_member():
    """The inverse: sorting must normalize ORDER, never CONTENT. A fence that
    only proved reordering is inert would be satisfied by `checks = []`."""
    live = _raw_live()
    live["required_status_checks"]["contexts"] = ["Backend Checks", "Something Else"]
    r = _run_check(live)
    assert _verdict(r) == "drifted", f"{r.stdout}{r.stderr}"


def test_f18_sorting_reaches_lists_of_objects_not_only_strings():
    mod = _normalize_module()
    a = mod.normalize({"checks": [{"context": "B", "app_id": 2},
                                  {"context": "A", "app_id": 1}]})
    b = mod.normalize({"checks": [{"context": "A", "app_id": 1},
                                  {"context": "B", "app_id": 2}]})
    assert a == b, "a list of dicts was not order-normalized"
    assert len(a["checks"]) == 2, "sorting dropped an element"


# ---------------------------------------------------------------------------
# Workflow fences. F21 first: without a non-vacuity baseline a job rename empties
# every collection below and F6-F15 pass while asserting nothing.
# ---------------------------------------------------------------------------
def _doc() -> dict:
    return yaml.safe_load(_artifact(WORKFLOW).read_text())


def _triggers() -> dict:
    doc = _doc()
    # ⚠ `yaml.safe_load` parses the bare key `on:` as the BOOLEAN True, not the
    # string "on". `doc.get("on", {})` silently yields {} and every trigger
    # assertion below would pass vacuously. Read both spellings.
    on = doc.get("on") if doc.get("on") is not None else doc.get(True)
    assert on, "could not read the `on:` block (the YAML-1.1 `on` -> True trap?)"
    return on


def _job() -> dict:
    return _doc()["jobs"]["probe"]


def _steps() -> list[dict]:
    return list(_job()["steps"])


def test_f21_the_workflow_is_shaped_as_this_module_assumes():
    doc = _doc()
    assert "probe" in doc.get("jobs", {}), f"jobs were {list(doc.get('jobs', {}))}"
    assert len(_steps()) >= 5, f"parsed only {len(_steps())} step(s)"
    assert _triggers(), "no triggers parsed"


def test_f7_the_push_trigger_is_unfiltered():
    """⚠⚠ Revision 1 guarded this with a YAML COMMENT ONLY. In a repo whose
    standing rule is *a grep can be satisfied by a comment*, the load-bearing
    property had no test -- and copying a sibling probe's paths-filtered `on:`
    block (`backup-freshness-probe.yml` has exactly one) would kill the primary
    detection path with every other fence green.

    The suspected primary cause PRODUCES a push to `main`, so unfiltered push is
    what collapses detection from 9 days to about a minute."""
    on = _triggers()
    push = on.get("push")
    assert isinstance(push, dict), f"push trigger was {push!r}"
    assert push.get("branches") == ["main"], push
    assert "paths" not in push, (
        "a `paths:` filter on the push trigger silently disables the primary "
        "detection path -- the probe would only fire when the probe itself changed")
    assert "paths-ignore" not in push, push


def test_f7_the_schedule_backstop_exists():
    """Public-repo scheduled workflows are auto-disabled after 60 days of
    inactivity, so the cron is the backstop, not the mechanism -- but a backstop
    that does not exist is not a backstop."""
    on = _triggers()
    schedule = on.get("schedule")
    assert isinstance(schedule, list) and schedule, f"schedule was {schedule!r}"
    assert all("cron" in entry for entry in schedule), schedule
    assert "workflow_dispatch" in on


def test_f14_cancel_in_progress_is_the_parsed_boolean_false():
    """⚠ The PARSED boolean, not the raw text. A run does not end at the fetch,
    it ends at the `gh issue` write, and cancellation between those points
    destroys an already-earned alarm: floor disarmed -> run A confirms drifted ->
    a later merge kills run A mid-notify -> run B reads a healed floor and reports
    in-posture. Observed drift, zero alarms."""
    concurrency = _doc().get("concurrency")
    assert isinstance(concurrency, dict), f"concurrency was {concurrency!r}"
    assert concurrency.get("cancel-in-progress") is False, (
        f"cancel-in-progress parsed as {concurrency.get('cancel-in-progress')!r}; "
        "it must be the boolean False")
    assert concurrency.get("group")


def test_f15_issues_write_is_on_the_job_not_the_workflow():
    """Job-level `permissions:` REPLACES workflow-level. A workflow-level grant
    with a job-level block that omits it leaves the notifier unable to write."""
    job = _job()
    perms = job.get("permissions")
    assert isinstance(perms, dict), f"job permissions were {perms!r}"
    assert perms.get("issues") == "write", perms
    assert perms.get("contents") == "read", perms


def _normalize_expr(expr) -> str:
    """Parse, do not raw-string match. `if: x != 'y'` and `if: ${{ x != 'y' }}`
    are both legitimate spellings, so an exact-string fence is over-specified and
    goes red on a correct edit -- and a red fence gets weakened, not obeyed."""
    s = str(expr).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", s, re.DOTALL)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", " ", s).strip().replace('"', "'")


def test_f6_both_gated_steps_gate_on_not_in_posture():
    """⚠ `if: verdict == 'drifted'` is SILENT on `could-not-run`, so an expired
    App credential becomes permanent invisible blindness: the probe stops working
    and nothing says so.

    ⚠ Asserted on BOTH steps BY ID. A generic "every `if:` looks right" sweep
    passes when one of the two steps loses its gate entirely."""
    steps = {s.get("id"): s for s in _steps() if s.get("id")}
    for step_id in ("alarm", "fail"):
        assert step_id in steps, (
            f"no step with id {step_id!r}; ids were {sorted(steps)}. Both the "
            "notify step and the fail step must be identifiable.")
        expr = _normalize_expr(steps[step_id].get("if", ""))
        assert expr, f"step {step_id!r} carries no `if:` gate at all"
        assert "!=" in expr and "in-posture" in expr, (
            f"step {step_id!r} gates on {expr!r}; it must gate on "
            "`!= 'in-posture'`, never on `== 'drifted'`.")
        assert "drifted" not in expr, f"step {step_id!r} gates on drift: {expr!r}"


def test_f6_the_expression_normalizer_accepts_both_legitimate_spellings():
    """Non-vacuity of the parser the fence above depends on."""
    bare = _normalize_expr("steps.check.outputs.verdict != 'in-posture'")
    wrapped = _normalize_expr("${{  steps.check.outputs.verdict   != \"in-posture\" }}")
    assert bare == wrapped, (bare, wrapped)
    assert "==" in _normalize_expr("${{ x == 'drifted' }}")


def test_f6_the_auth_step_is_continue_on_error():
    """⚠ So a revoked App or rotated key becomes a LOUD `could-not-run`, not a
    raw red square nobody opens. Removing key expiry does not make the mint
    infallible."""
    auth = [s for s in _steps()
            if "app-token" in str(s.get("uses", "")) or s.get("id") == "auth"]
    assert auth, f"no auth step found among {[s.get('name') for s in _steps()]}"
    assert all(s.get("continue-on-error") is True for s in auth), auth


def test_f6_a_failed_auth_becomes_a_verdict_not_a_crash():
    """The consequence of continue-on-error: something downstream must TURN the
    failed outcome into `could-not-run`, or the step just succeeds emptily."""
    # ⚠ COMMENT-STRIPPED. This module's header promises stripping and this fence
    # was reading the workflow raw -- `assert "could-not-run" in body` was
    # already satisfied by the header comment that EXPLAINS the could-not-run
    # behaviour. A grep satisfied by the comment describing the thing it greps
    # for is this repo's signature defect.
    body = "\n".join(ln for ln in _artifact(WORKFLOW).read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "steps.auth.outcome" in body, (
        "nothing reads the auth step's outcome, so continue-on-error only hides "
        "the failure instead of converting it into an alarm")
    assert "could-not-run" in body


def test_the_workflow_declares_the_coverage_boundary():
    """⚠ A monitor that overstates its coverage is worse than none, because the
    next TBD-404 will cite it. `allow_merge_commit` / `allow_rebase_merge` live
    on `GET /repos/{o}/{r}`, NOT on `/protection`, so re-enabling merge commits
    leaves this probe green -- and the entire release pipeline depends on
    squash-only, because the squash subject IS the string semantic-release
    parses."""
    body = _artifact(WORKFLOW).read_text()
    assert "allow_merge_commit" in body, (
        "the workflow header must name the most important uncovered setting")


# ---------------------------------------------------------------------------
# W4. The notifier, EXECUTED. Every reference to it was textual.
# ---------------------------------------------------------------------------
def _fake_gh_issue(tmp_path, *, existing="", create_ok=True,
                   label_create_ok=True, comment_ok=True) -> pathlib.Path:
    """A `gh` stand-in for the issue plumbing, logging every argv."""
    state = tmp_path / "issue-state"
    state.mkdir(exist_ok=True)
    (state / "cfg.json").write_text(json.dumps({
        "existing": existing, "create_ok": create_ok,
        "label_create_ok": label_create_ok, "comment_ok": comment_ok}))
    gh = tmp_path / "fake-gh-issue"
    gh.write_text(f'''#!/usr/bin/env python3
import json, pathlib, sys
state = pathlib.Path({str(state)!r})
c = json.loads((state / "cfg.json").read_text())
args = sys.argv[1:]
with (state / "argv.log").open("a") as fh:
    fh.write(json.dumps(args) + "\\n")
if args[:2] == ["issue", "list"]:
    # ⚠ The stub APPLIES the notifier's own --jq filter. An unfiltered stub
    # returns whatever it is given, so the safe (`startswith`) and unsafe
    # (`.[0].number`) idioms become indistinguishable and a C6 fence could not
    # express its mutant. Same class as the stub that ignored --jq and made the
    # F20 canary vacuous.
    jq = ""
    for k, a in enumerate(args):
        if a == "--jq" and k + 1 < len(args):
            jq = args[k + 1]
    try:
        rows = json.loads(c["existing"])
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        sys.stdout.write(c["existing"])
        sys.exit(0)
    prefix = None
    marker = 'startswith("'
    if marker in jq:
        rest = jq.split(marker, 1)[1]
        prefix = rest.split('"', 1)[0]
    nums = [str(r.get("number")) for r in rows
            if prefix is None or str(r.get("title", "")).startswith(prefix)]
    if ".[0].number" in jq:
        nums = nums[:1]
    sys.stdout.write(chr(10).join(nums) + (chr(10) if nums else ""))
    sys.exit(0)
if args[:2] == ["issue", "comment"]:
    sys.exit(0 if c["comment_ok"] else 1)
if args[:2] == ["issue", "create"]:
    if "--label" in args:
        sys.exit(0 if c["label_create_ok"] else 1)
    sys.exit(0 if c["create_ok"] else 1)
sys.exit(0)
''')
    gh.chmod(0o755)
    return gh


def _run_notifier(tmp_path, gh, **env_overrides):
    env = {"GH_TOKEN": "t", "GH_REPO": "flamarion/tbd", "RUN_ID": "42",
           "VERDICT": "drifted", "DETAIL": "enforce_admins flipped"}
    env.update(env_overrides)
    env = {k: v for k, v in env.items() if v is not None}
    # The notifier calls bare `gh`; put the stub first on PATH.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gh").write_bytes(gh.read_bytes())
    (bindir / "gh").chmod(0o755)
    return subprocess.run(
        ["bash", str(_artifact(NOTIFIER))], capture_output=True, text=True,
        env={**os.environ, **env, "PATH": f"{bindir}:{os.environ['PATH']}"})


def _issue_calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "issue-state" / "argv.log"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("missing", ["GH_TOKEN", "GH_REPO", "RUN_ID", "VERDICT"])
def test_w4_the_notifier_refuses_when_a_required_input_is_missing(tmp_path, missing):
    """⚠⚠ The required-input loop had ZERO executable coverage: invert its `-z`
    to `-n` and nothing noticed. It is not decoration -- the workflow's alarm step
    dies on it, and until this round that also silently skipped the fail step."""
    gh = _fake_gh_issue(tmp_path)
    r = _run_notifier(tmp_path, gh, **{missing: ""})
    assert r.returncode == 2, f"{missing}: exit {r.returncode}\n{r.stdout}{r.stderr}"
    assert missing in r.stderr
    assert not _issue_calls(tmp_path), "it contacted GitHub despite bad inputs"


def test_w4_the_notifier_opens_an_issue_when_none_exists(tmp_path):
    gh = _fake_gh_issue(tmp_path, existing="")
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    calls = _issue_calls(tmp_path)
    created = [c for c in calls if c[:2] == ["issue", "create"]]
    assert created, f"no issue was created: {calls}"
    flat = " ".join(created[0])
    assert "--repo" in flat and "flamarion/tbd" in flat, (
        f"a lost --repo would open the issue on the WRONG repository: {flat}")
    assert "[branch-protection]" in flat


def test_c6_the_dedupe_does_not_capture_an_unrelated_issue(tmp_path):
    """⚠⚠ GitHub's `in:title` search is an AND over TOKENS, not a literal prefix
    -- probed live, `in:title production` returns the deploy-drift issue. So a
    search for `[branch-protection]` also matches any open issue whose title
    contains "branch" and "protection", and taking `.[0].number` blindly would
    post this alarm as a comment on somebody else's incident, where nobody is
    looking for it. `notify-backup-stale.sh` already had the safe idiom; the
    weaker `notify-deploy-drift.sh` form was copied here.

    ⚠ F8 asserts the five dedupe LITERALS are pairwise distinct. That says
    nothing about what a fuzzy title search matches, so F8 is not a fence on
    this and must not be mistaken for one.

    The stub returns a fuzzy match the real API would return: an unrelated issue
    whose title merely contains the words."""
    fuzzy = json.dumps([
        {"number": 725, "title": "[deploy-drift] production branch protection is stale"},
        {"number": 900, "title": "branch protection question"},
    ])
    gh = _fake_gh_issue(tmp_path, existing=fuzzy)
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    calls = _issue_calls(tmp_path)
    commented = [c for c in calls if c[:2] == ["issue", "comment"]]
    assert not commented, (
        "the alarm was posted onto an unrelated issue that merely matched the "
        f"search tokens: {commented}")
    assert any(c[:2] == ["issue", "create"] for c in calls), calls


def test_c6_the_dedupe_still_finds_its_own_issue(tmp_path):
    """The inverse, so the fence above cannot be satisfied by never deduping at
    all -- which would open a fresh issue on every push to main."""
    mine = json.dumps([
        {"number": 725, "title": "[deploy-drift] production is not serving"},
        {"number": 42, "title": "[branch-protection] main's protection no longer matches"},
    ])
    gh = _fake_gh_issue(tmp_path, existing=mine)
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    calls = _issue_calls(tmp_path)
    assert any(c[:2] == ["issue", "comment"] and "42" in c for c in calls), calls
    assert not any(c[:2] == ["issue", "create"] for c in calls), calls


def test_w4_the_notifier_comments_rather_than_duplicating(tmp_path):
    """The dedupe branch. `EXISTING != "null"` is the guard that stops a new
    issue per run; it had no coverage at all."""
    gh = _fake_gh_issue(tmp_path, existing="77\n")
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    calls = _issue_calls(tmp_path)
    assert any(c[:2] == ["issue", "comment"] and "77" in c for c in calls), calls
    assert not any(c[:2] == ["issue", "create"] for c in calls), (
        f"it opened a duplicate issue instead of commenting: {calls}")


def test_w4_a_literal_null_from_gh_is_not_an_issue_number(tmp_path):
    """⚠ `gh --jq '.[0].number'` prints the STRING `null` when the list is empty.
    Without the `!= "null"` guard the notifier would comment on issue "null",
    fail, and exit 1 -- an alarm that never lands."""
    gh = _fake_gh_issue(tmp_path, existing="null\n")
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    calls = _issue_calls(tmp_path)
    assert any(c[:2] == ["issue", "create"] for c in calls), calls


def test_w4_the_notifier_falls_back_when_the_label_does_not_exist(tmp_path):
    """The labelled create fails on a repo without the label; the signal must
    still land. Kills "drop the fallback"."""
    gh = _fake_gh_issue(tmp_path, label_create_ok=False, create_ok=True)
    r = _run_notifier(tmp_path, gh)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    creates = [c for c in _issue_calls(tmp_path) if c[:2] == ["issue", "create"]]
    assert len(creates) == 2, f"expected a labelled attempt then a bare one: {creates}"
    assert "--label" in " ".join(creates[0]) and "--label" not in " ".join(creates[1])


def test_w4_the_notifier_reports_failure_when_it_cannot_open_anything(tmp_path):
    """The inverse: if BOTH creates fail it must exit non-zero, or a silent
    notifier reads as a delivered alarm."""
    gh = _fake_gh_issue(tmp_path, label_create_ok=False, create_ok=False)
    r = _run_notifier(tmp_path, gh)
    assert r.returncode != 0, f"{r.stdout}{r.stderr}"


def test_w4_a_failed_comment_is_reported(tmp_path):
    gh = _fake_gh_issue(tmp_path, existing="77\n", comment_ok=False)
    r = _run_notifier(tmp_path, gh)
    assert r.returncode != 0, f"{r.stdout}{r.stderr}"


def test_w4_the_body_carries_the_verdict_and_the_coverage_boundary(tmp_path):
    """The issue body is the whole product of this alarm."""
    gh = _fake_gh_issue(tmp_path)
    _run_notifier(tmp_path, gh, VERDICT="could-not-run", DETAIL="token revoked")
    body = " ".join(" ".join(c) for c in _issue_calls(tmp_path))
    assert "could-not-run" in body and "token revoked" in body
    assert "allow_merge_commit" in body, (
        "the alarm must carry the coverage boundary; a reader acting on it will "
        "otherwise assume the probe covers merge-method settings")


# ---------------------------------------------------------------------------
# W9. The normalizer's CLI -- the documented regeneration path.
# ---------------------------------------------------------------------------
def _run_normalizer(payload, tmp_path):
    return subprocess.run(
        ["python3", str(_artifact(NORMALIZER))], input=payload,
        capture_output=True, text=True, cwd=str(tmp_path))


def test_w9_the_normalizer_cli_reproduces_the_committed_posture(tmp_path):
    """⚠⚠ `main()` had ZERO tests: every other fence imports the module under a
    different name, so `__main__` never fires. Yet this CLI IS the documented
    regeneration path -- named in `.github/branch-protection/README.md`, in the
    checker's own drift message, and in CLAUDE.md. A regeneration path that
    emits a differently-formatted document hands the operator a diff of pure
    formatting noise, and the trained response to that is to stop reading it."""
    r = _run_normalizer(json.dumps(_raw_live()), tmp_path)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    assert r.stdout == _artifact(POSTURE).read_text(), (
        "the documented regeneration command does not reproduce the committed "
        "posture byte for byte")


def test_w9_the_normalizer_cli_writes_to_no_path(tmp_path):
    """It emits on stdout and the operator redirects. Run it in an empty
    directory and prove the directory is still empty."""
    work = tmp_path / "empty"
    work.mkdir()
    r = _run_normalizer(json.dumps(_raw_live()), work)
    assert r.returncode == 0
    assert list(work.iterdir()) == [], [p.name for p in work.iterdir()]


@pytest.mark.parametrize("payload,label", [
    ("", "empty stdin"), ("   \n", "whitespace"), ("not json", "non-JSON")])
def test_w9_the_normalizer_cli_refuses_bad_input(payload, label, tmp_path):
    r = _run_normalizer(payload, tmp_path)
    assert r.returncode == 2, f"{label}: exit {r.returncode}\n{r.stdout}{r.stderr}"
    assert not r.stdout.strip(), f"{label}: emitted a document anyway"


def test_w9_the_normalizer_cli_output_is_stable_and_sorted(tmp_path):
    """`indent=2, sort_keys=True` is what makes the diff readable. Drop either
    and every future regeneration is a whole-file rewrite."""
    r = _run_normalizer(json.dumps(_raw_live()), tmp_path)
    assert r.stdout.endswith("\n")
    lines = r.stdout.splitlines()
    top = [ln for ln in lines if re.match(r'^  "[a-z_]+":', ln)]
    keys = [re.match(r'^  "([a-z_]+)":', ln).group(1) for ln in top]
    assert keys == sorted(keys), f"top-level keys are not sorted: {keys}"
    assert any(ln.startswith("  ") for ln in lines), "output is not indented"
    again = _run_normalizer(json.dumps(_raw_live()), tmp_path)
    assert again.stdout == r.stdout, "the CLI is not deterministic"


# ---------------------------------------------------------------------------
# W1/W2/W3. The alarm and fail steps' BODIES, not just their `if:`.
# ---------------------------------------------------------------------------
def test_w1_the_alarm_step_actually_invokes_the_notifier():
    """⚠⚠ Replace the alarm step's `run:` with `echo drift` and F6, F8, F9c and
    F21 all stay GREEN: drift detected, no issue ever opened. The `if:` was
    fenced and the body was not.

    House precedent, three files away -- `test_backup_offhead`'s sibling
    `test_backup_offhost.py:367` treats exactly this as load-bearing:

        alarm = [s for s in steps if "notify-backup-stale.sh" in str(s.get("run", ""))]
        assert alarm, "the workflow never invokes the alarm script."
    """
    steps = {st.get("id"): st for st in _steps() if st.get("id")}
    assert "alarm" in steps, f"ids were {sorted(steps)}"
    run = str(steps["alarm"].get("run", ""))
    assert "notify-protection-drift.sh" in run, (
        f"the alarm step does not invoke the notifier; it runs: {run!r}. Drift "
        "would be detected and no issue would ever be opened.")
    assert run.strip().startswith("bash ") or "bash " in run, run


def test_w3_the_alarm_step_passes_every_input_the_notifier_hard_requires():
    """⚠⚠ `notify-protection-drift.sh:27-32` hard-exits 2 if ANY of
    GH_TOKEN/GH_REPO/RUN_ID/VERDICT is empty. Fencing only `GH_TOKEN` leaves the
    other three droppable, and dropping one kills the alarm step -- which used to
    also SKIP the fail step, because Actions implicitly ANDs `success()` into an
    `if:` with no status function. Drift detected, no issue, bare red square.

    The required set is read FROM THE NOTIFIER, so the two can never drift."""
    required = set()
    for line in _executable_lines(NOTIFIER):
        m = re.match(r"\s*for\s+\w+\s+in\s+([A-Z_ ]+);\s*do", line)
        if m:
            required.update(m.group(1).split())
    assert required, (
        "could not parse the notifier's required-input loop; if its shape "
        "changed, update this fence rather than deleting it")
    steps = {st.get("id"): st for st in _steps() if st.get("id")}
    env = {k: str(v) for k, v in (steps["alarm"].get("env") or {}).items()}
    missing = sorted(required - set(env))
    assert not missing, (
        f"the alarm step does not pass {missing}, and the notifier hard-exits 2 "
        f"without them. It passes {sorted(env)}.")
    for name in required:
        assert env[name].strip(), f"{name} is passed but empty"


def test_w3_the_fail_step_runs_even_when_the_alarm_step_failed():
    """⚠⚠ Without a status function, Actions ANDs `success()` in, so a failed
    alarm step SKIPS this one and the only signal left is a red square."""
    steps = {st.get("id"): st for st in _steps() if st.get("id")}
    expr = _normalize_expr(steps["fail"].get("if", ""))
    assert "always()" in expr or "!cancelled()" in expr, (
        f"the fail step's gate is {expr!r}. With no status function Actions "
        "implicitly requires success(), so this step is skipped exactly when the "
        "alarm step died -- the case where failing loudly matters most.")


def test_w2_the_fail_step_body_actually_fails(tmp_path):
    """⚠⚠ F6 checked this step's `if:` and stopped. Replace its body with
    `echo "verdict: X"` and drift is detected, the job goes GREEN, and every
    fence passes. So RUN the body and assert a non-zero exit."""
    steps = {st.get("id"): st for st in _steps() if st.get("id")}
    script = str(steps["fail"]["run"]).replace(
        "${{ steps.check.outputs.verdict }}", "drifted")
    assert "${{" not in script, script
    path = tmp_path / "fail-step.sh"
    path.write_text(script)
    r = subprocess.run(["bash", str(path)], capture_output=True, text=True)
    assert r.returncode != 0, (
        "the fail step exits 0, so a drifted verdict leaves the job GREEN:\n"
        f"{script}")
    assert "drifted" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# F8. Kills: a shared dedupe bucket. One alarm silencing another.
# ---------------------------------------------------------------------------
def _effective_dedupe_tokens(path: pathlib.Path) -> set[str]:
    """The bracket token a notifier ACTUALLY deduplicates on.

    ⚠⚠ THE HOUSE HAS TWO IDIOMS AND A FENCE THAT KNOWS ONLY ONE IS VACUOUS.
    Measured: only `notify-deploy-drift.sh:29` and `notify-backup-stale.sh:15`
    define `TITLE_PREFIX`; `notify-smoke-failure.sh:42` and
    `notify-undeployed-release.sh:73` hardcode `TITLE` and a SEPARATE
    `--search '"[...]" in:title'` literal. A fence collecting `TITLE_PREFIX=`
    assignments finds three values, asserts they differ, and PASSES -- while a
    new notifier copied from the other idiom with `[smoke-fail]` still in its
    search line deduplicates straight into the smoke-failure issue and produces
    ZERO signal during an incident.

    ⚠ The `--search` line is the authority, because that is what actually
    performs the dedupe. But two of the four spell it with a VARIABLE
    (`--search "in:title ${TITLE_PREFIX}"`), so the token must be resolved
    through one level of indirection -- the spec's "literals actually used in
    the search calls" do not literally exist for half the corpus.
    """
    text = path.read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    search_lines = [ln for ln in code if "--search" in ln]
    assert search_lines, f"{path.name} performs no `gh issue list --search` dedupe"
    tokens = set()
    for ln in search_lines:
        tokens.update(re.findall(r"\[[a-z0-9-]+\]", ln))
        if re.search(r"\$\{?TITLE_PREFIX\}?", ln):
            for decl in code:
                if re.match(r"\s*TITLE_PREFIX\s*=", decl):
                    tokens.update(re.findall(r"\[[a-z0-9-]+\]", decl))
    assert tokens, (
        f"{path.name} has a --search line but no resolvable bracket token: "
        f"{search_lines}")
    return tokens


def test_f8_every_notifier_dedupe_bucket_is_pairwise_distinct():
    """⚠ Revision 1 named this hazard in prose and wrote no test, so copying
    `notify-deploy-drift.sh` and leaving the bucket unchanged would make each
    alarm silence the other -- the branch-protection alarm landing as a comment
    on the open deploy-drift issue, where nobody looks for it."""
    root = REPO_ROOT
    notifiers = sorted(_scripts_dir().glob("notify-*.sh"))
    # ⚠ A FLOOR, NOT AN EQUALITY. An exact count reds THIS module when an
    # unrelated PR adds a sixth notifier anywhere in the repo -- a fence that
    # fails for reasons outside its own subject is one that gets deleted. The
    # property is pairwise distinctness over whatever exists; the floor only
    # guarantees the corpus was actually found.
    assert len(notifiers) >= 5, (
        f"expected at least the 5 known notifiers, found "
        f"{[p.name for p in notifiers]}; this fence searched the wrong tree.")
    buckets = {p.name: _effective_dedupe_tokens(p) for p in notifiers}
    seen: dict[str, str] = {}
    for name, tokens in buckets.items():
        for tok in tokens:
            assert seen.get(tok, name) == name, (
                f"dedupe bucket {tok} is shared by {seen[tok]} and {name} -- one "
                "alarm would silence the other")
            seen[tok] = name
    assert seen.get("[branch-protection]") == "notify-protection-drift.sh", (
        f"the new notifier does not dedupe on its own bucket; buckets were {buckets}")


def test_f8_the_resolver_sees_both_house_idioms():
    """⚠ Non-vacuity of the resolver itself. If it silently returned an empty set
    for the `TITLE=`-only idiom, the fence above would compare two tokens instead
    of five and pass while proving almost nothing."""
    root = REPO_ROOT
    for name, expected in (
        ("notify-smoke-failure.sh", "[smoke-fail]"),
        ("notify-undeployed-release.sh", "[undeployed-release]"),
        ("notify-deploy-drift.sh", "[deploy-drift]"),
        ("notify-backup-stale.sh", "[backup-stale]"),
    ):
        tokens = _effective_dedupe_tokens(_scripts_dir() / name)
        assert expected in tokens, f"{name}: resolved {tokens}, expected {expected}"


def test_f8_the_notifier_never_auto_closes():
    body = "\n".join(_executable_lines(NOTIFIER))
    for verb in ("issue close", "--state closed", "issue edit"):
        assert verb not in body, f"the notifier auto-closes ({verb!r}); it must not"


def test_the_notifier_requires_its_inputs_rather_than_failing_silently():
    body = "\n".join(_executable_lines(NOTIFIER))
    for var in ("GH_TOKEN", "GH_REPO", "RUN_ID", "VERDICT"):
        assert var in body, f"the notifier never reads {var}"


