"""Fences for the data-plane package pins (TBD-419).

⚠⚠ EVERY ASSERTION HERE PARSES THE YAML. None of them greps.

`roles/common/tasks/main.yml` now carries a long comment block explaining why
`upgrade: safe` is disarmed -- and that comment contains the literal string
`upgrade: safe`. A whole-file grep for it is therefore satisfied by the comment
documenting its own absence, which is the exact trap that has bitten this repo
three times in two days (TBD-433, TBD-434). `yaml.safe_load` drops comments, so
parsing is the only form of this check that means anything.

⚠ These fences can only assert that the role CALLS the apt-policy filters. They
structurally cannot tell a correct parse from `'8.4' in stdout`. That job
belongs to `test_apt_policy_filter.py`, which is why the parse was moved into a
Python module in the first place. Do not add a "checks the parse" assertion
here; it would be decoration.

The hazard being fenced: `ansible.builtin.apt` with `upgrade: safe` ran
unconditionally as the first task of the first role, so converging a Redis knob
also performed an unbounded package upgrade on the production database droplet.
"""

import configparser
import os
import pathlib
import re

import pytest
import yaml


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / "infra" / "ansible" / "playbooks" / "site.yml").exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "infra/ansible/playbooks/site.yml not found from a CI checkout; these "
        "fences must not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason=(
        "the infra tree is not mounted into the backend container; run "
        "`docker compose up -d --force-recreate backend` to pick up the "
        "./infra:/app/infra:ro mount. Always runs in CI."
    ),
)


def ANSIBLE() -> pathlib.Path:
    return REPO_ROOT / "infra" / "ansible"


# ⚠ A SYNTHETIC KEY `_flatten` STAMPS ONTO CHILDREN OF AN ERROR-SWALLOWING
# ANCESTOR. It must be in KEYWORDS below or `_module()` would return it as the
# module name for every such task and silently break every `_is()` check.
_INHERITED = "_tbd_inherited_error_swallowing"

# Task-level keywords, so whatever key is left over is the module being called.
KEYWORDS = {
    "name", "when", "notify", "register", "changed_when", "failed_when", "loop",
    "with_items", "with_dict", "loop_control", "become", "become_user", "tags",
    "vars", "args", "ignore_errors", "no_log", "block", "rescue", "always",
    "check_mode", "delegate_to", "run_once", "environment", "until", "retries",
    "delay", "listen", "any_errors_fatal", "throttle", "diff", "connection",
    "remote_user", "module_defaults", "collections", "poll", "async",
    "delegate_facts", "ignore_unreachable", _INHERITED,
}


def _load(path: pathlib.Path):
    return yaml.safe_load(path.read_text()) or []


def _flatten(tasks, inherited: frozenset[str] = frozenset()) -> list[dict]:
    """Task list with `block:`/`rescue:`/`always:` children pulled up.

    ⚠ Without this, wrapping a re-added `upgrade: safe` in a `block:` would hide
    it from every fence below while changing nothing about what runs.

    ⚠⚠ AND WITHOUT THE `inherited` THREADING, pulling a child up STRIPS ITS
    ANCESTORS' ERROR HANDLING. Measured green before this was added: wrapping
    the `dpkg_selections` hold task in a `block:` carrying `ignore_errors: true`
    left the whole suite passing, because the child dict that reached
    `test_the_hold_loop_is_host_derived_and_does_not_swallow_errors` had no
    `ignore_errors` key of its own -- so the negative check that exists to kill
    exactly that edit saw nothing. A `rescue:` does the same thing by a
    different route. Both are named in that test's own docstring as the
    implementations it kills, and neither one killed.

    `ignore_errors` on a parent covers everything under it; a `rescue:` covers
    only the `block:` half, so the two are threaded separately.
    """
    out: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if inherited:
            task = {**task, _INHERITED: sorted(inherited)}
        out.append(task)
        down = set(inherited)
        if task.get("ignore_errors"):
            down.add("ignore_errors on an ancestor block")
        rescued = down | (
            {"rescue: on an ancestor block"}
            if isinstance(task.get("rescue"), list) and task["rescue"]
            else set()
        )
        for section, carried in (
            ("block", rescued), ("rescue", down), ("always", down)
        ):
            if isinstance(task.get(section), list):
                out.extend(_flatten(task[section], frozenset(carried)))
    return out


def _module(task: dict) -> str | None:
    for key in task:
        if key not in KEYWORDS:
            return key
    return None


def _args(task: dict) -> dict:
    """The module's argument mapping, or {} for free-form / non-mapping args."""
    module = _module(task)
    args = task.get(module) if module else None
    return args if isinstance(args, dict) else {}


def _is(task: dict, *names: str) -> bool:
    module = _module(task)
    return module is not None and module.split(".")[-1] in names


def _swallows_errors(task: dict) -> list[str]:
    """Every reason this task's failure would not fail the play."""
    reasons = list(task.get(_INHERITED) or [])
    if task.get("ignore_errors"):
        reasons.append("ignore_errors")
    if "failed_when" in task:
        reasons.append("failed_when")
    return reasons


def _tags(task: dict) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _applied_tags(task: dict) -> set[str]:
    """Tags a dynamic include pushes down onto the tasks it pulls in.

    Covers `include_tasks` AND `include_role`: `site.yml`'s post_tasks pass and
    the mysql role's re-hold both use the latter, and a version of this helper
    that only knew about `include_tasks` returned an empty set for them --
    which, checked with `in`, is indistinguishable from "not tagged".
    """
    args = _args(task)
    tags = (args.get("apply") or {}).get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _every_task_file() -> dict[str, list[dict]]:
    """Every file in the tree that ansible will execute tasks from.

    ⚠ ROLE HANDLERS ARE IN HERE, and leaving them out was a live gap: measured
    green, an `apt: {upgrade: dist}` added to `roles/common/handlers/main.yml`
    passed the whole suite, because the glob was `roles/*/tasks/*.yml` and role
    handler files were never parsed at all. A handler is a task; it runs on the
    same box, with the same privileges, when anything notifies it.
    """
    files = {}
    for pattern in ("roles/*/tasks/*.yml", "roles/*/handlers/*.yml"):
        for path in sorted(ANSIBLE().glob(pattern)):
            files[str(path.relative_to(ANSIBLE()))] = _flatten(_load(path))
    for path in sorted(ANSIBLE().glob("playbooks/*.yml")):
        doc = _load(path)
        tasks = []
        for play in doc if isinstance(doc, list) else [doc]:
            if isinstance(play, dict):
                for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    tasks.extend(_flatten(play.get(section)))
        files[str(path.relative_to(ANSIBLE()))] = tasks
    return files


def _module_defaults() -> list[tuple[str, dict]]:
    """Every `module_defaults:` mapping in the tree, wherever it is attached.

    ⚠⚠ `module_defaults` IS A KEYWORD, NOT A MODULE. `_module()` steps over it
    and `_every_task_file()` never looks at play-level keys at all, so an
    `allow_change_held_packages` set through it is invisible to a sweep of
    module arguments -- while applying to every `apt` task in scope. Measured
    green: adding it as a play-level `module_defaults` in `site.yml` left the
    whole suite passing with the pin comprehensively defeated. Block-level and
    `roles: [{role: x, module_defaults: ...}]` are the same hole.
    """
    found: list[tuple[str, dict]] = []

    def take(where: str, node) -> None:
        if isinstance(node, dict):
            found.append((where, node))

    for path in sorted(ANSIBLE().glob("playbooks/*.yml")):
        rel = str(path.relative_to(ANSIBLE()))
        doc = _load(path)
        for play in doc if isinstance(doc, list) else [doc]:
            if not isinstance(play, dict):
                continue
            take(f"{rel} (play-level)", play.get("module_defaults"))
            for entry in play.get("roles") or []:
                if isinstance(entry, dict):
                    take(f"{rel} (roles: {entry.get('role')})",
                         entry.get("module_defaults"))
    for name, tasks in _every_task_file().items():
        for task in tasks:
            take(f"{name}: {task.get('name')!r}", task.get("module_defaults"))
    return found


def COMMON() -> list[dict]:
    return _flatten(_load(ANSIBLE() / "roles" / "common" / "tasks" / "main.yml"))


def HOLDS() -> list[dict]:
    return _flatten(_load(ANSIBLE() / "roles" / "common" / "tasks" / "holds.yml"))


def FENCE() -> list[dict]:
    return _flatten(_load(ANSIBLE() / "roles" / "common" / "tasks" / "mysql_track_fence.yml"))


def DEFAULTS() -> dict:
    return _load(ANSIBLE() / "roles" / "common" / "defaults" / "main.yml")


def SITE() -> dict:
    return _load(ANSIBLE() / "playbooks" / "site.yml")[0]


def _index(tasks: list[dict], predicate) -> int | None:
    for i, task in enumerate(tasks):
        if predicate(task):
            return i
    return None


# `apt-get upgrade`, `apt-get dist-upgrade`, `apt full-upgrade`, `aptitude
# safe-upgrade`, in any order of flags.
#
# ⚠ NO `--only-upgrade` EXEMPTION. The first cut carried a
# `(?!.*--only-upgrade)` lookahead, on the reasoning that a single-package
# `--only-upgrade` is what MIGRATION.md's deliberate-move procedure uses. That
# procedure is run BY HAND, in a window, after a snapshot -- it is not a task in
# this play, so the exemption protected nothing here and bought a trivial
# evasion: appending `--only-upgrade` anywhere in a `command:` string would have
# waved through the `dist-upgrade` next to it. A false positive here costs one
# review conversation; a false negative is an unbounded apt run on the
# production data droplet.
_SHELL_UPGRADE = re.compile(
    r"\b(?:apt|apt-get|aptitude)\b"
    r".*?\b(?:dist-upgrade|full-upgrade|safe-upgrade|upgrade)\b",
    re.S,
)


def _upgrades(task: dict) -> bool:
    """Does this task perform an unbounded package upgrade?

    ⚠ `command`/`shell` COUNT. Measured green before they did: an
    `ansible.builtin.command: apt-get -y dist-upgrade` added untagged to
    `roles/common/tasks/main.yml` passed the whole suite, because the sweep
    only understood `ansible.builtin.apt`. Re-arming the defect through a
    `command:` is the obvious route once the `apt` module's is fenced, and it
    is the same unbounded upgrade on the same production database droplet.
    """
    if _is(task, "apt"):
        return str(_args(task).get("upgrade", "no")).lower() not in (
            "no", "false", "none",
        )
    if _is(task, "command", "shell", "raw"):
        module = _module(task)
        raw = task.get(module)
        blob = " ".join(str(v) for v in raw.values()) if isinstance(raw, dict) else str(raw)
        return bool(_SHELL_UPGRADE.search(blob))
    return False


# --- positive baseline ------------------------------------------------------


def test_the_ansible_tree_is_shaped_as_this_module_assumes():
    """Without this, a rename or a glob typo empties every collection below and
    the whole file passes while asserting nothing."""
    files = _every_task_file()
    assert len(files) >= 6, f"only parsed {sorted(files)}"
    apt_tasks = [t for tasks in files.values() for t in tasks if _is(t, "apt")]
    assert len(apt_tasks) >= 3, f"parsed only {len(apt_tasks)} apt task(s)"
    assert len(COMMON()) >= 5, "common/tasks/main.yml did not parse into tasks"
    assert HOLDS(), "common/tasks/holds.yml did not parse into tasks"
    assert FENCE(), "common/tasks/mysql_track_fence.yml did not parse into tasks"
    assert "roles" in SITE(), "site.yml has no roles block"
    # The handler glob is new and carries a real fence (an `apt: upgrade: dist`
    # in a role handler used to be invisible). Without this, a typo in the
    # pattern silently reverts the widening.
    handlers = [n for n in files if "/handlers/" in n]
    assert len(handlers) >= 3, f"role handler files were not parsed: {sorted(files)}"
    assert any(files[n] for n in handlers), "every parsed handler file is empty"


def test_this_module_sees_through_a_block_that_swallows_errors():
    """Positive baseline for `_flatten`'s ancestor threading.

    `_swallows_errors` is a negative check used by the hold fence, and a
    negative check on a helper that silently returns nothing is the classic
    vacuous pass. This pins the helper against a synthetic tree, so the fence
    below cannot go quietly toothless if `_flatten` regresses.
    """
    tree = [
        {"name": "plain", "ansible.builtin.command": "true"},
        {"name": "ignoring", "ignore_errors": True, "block": [
            {"name": "child", "ansible.builtin.command": "true"},
        ]},
        {"name": "rescued", "block": [
            {"name": "guarded", "ansible.builtin.command": "true"},
        ], "rescue": [
            {"name": "handler", "ansible.builtin.command": "true"},
        ]},
    ]
    by_name = {t["name"]: t for t in _flatten(tree)}
    assert not _swallows_errors(by_name["plain"])
    assert _swallows_errors(by_name["child"]), "ignore_errors did not reach the child"
    assert _swallows_errors(by_name["guarded"]), "rescue: did not reach the block child"
    assert not _swallows_errors(by_name["handler"]), (
        "a task in the `rescue:` half is not itself rescued"
    )
    # `_module()` must still find the module through the synthetic marker.
    assert _is(by_name["child"], "command"), "the inherited marker shadowed the module"


# --- the upgrade task -------------------------------------------------------


def test_no_apt_task_upgrades_packages_outside_the_never_tag():
    """THE fence. Sweeps the WHOLE ansible tree, not just `common`, so adding a
    new role with an unconditional `upgrade:` is caught too.

    Kills: reverting to `upgrade: safe` on a task that runs by default, which is
    the literal defect this ticket exists to close. Also kills the half-fix of
    gating it on a variable -- a variable is settable from role defaults,
    group_vars, inventory.yml or run-playbook.sh's generated extra-vars file,
    none of which the operator sees. Only `never` requires typing it.

    ⚠⚠ THE TAG SET IS PINNED EXACTLY, and checking only "`never` is present" was
    NOT ENOUGH. Ansible's `evaluate_tags` reads:

        if 'always' in tags: should_run = True     # <- BEFORE it looks at never

    so `always` WINS OVER `never`. Measured: with `tags: [patch, never]` the
    task is skipped on a default converge; with `tags: [patch, never, always]`
    it RUNS -- restoring the exact unbounded `upgrade: safe` on every routine
    converge of the production data droplet, while the fence that exists to stop
    that stays green because `never` is still in the list. `[patch, never]` is
    the only tag set that means what this task's comment claims, so that is what
    is pinned.
    """
    offenders = [
        f"{name}: {task.get('name')!r} tags={sorted(_tags(task))}"
        for name, tasks in _every_task_file().items()
        for task in tasks
        if _upgrades(task) and _tags(task) != {"patch", "never"}
    ]
    assert not offenders, (
        "a task performs an unbounded package upgrade and is not tagged exactly "
        "`[patch, never]`, so a routine converge of the production database "
        "droplet can perform it again (TBD-419). ⚠ `always` beats `never` in "
        "ansible's own tag evaluation, so [patch, never, always] RUNS BY "
        f"DEFAULT: {offenders}"
    )
    # And the capability must still exist, or `--tags patch` silently does
    # nothing and OS patching quietly moves back to a hand-run ssh block.
    upgrades = [
        task for tasks in _every_task_file().values()
        for task in tasks if _upgrades(task)
    ]
    assert upgrades, (
        "no upgrade-capable task left anywhere in the tree; deleting the "
        "capability moves routine patching to an undeclared hand-run shell "
        "block, which is the defect class this ticket exists to kill"
    )


def test_the_apt_cache_is_refreshed_before_the_fence_on_every_invocation():
    """Kills: deleting the whole first task instead of only its `upgrade:` key.

    Silent and plausible. `Install baseline packages` and the track fence would
    then read whatever cache the DO image happened to ship with, so the fence
    would be answering a question about last week's repository.

    ⚠⚠ AND KILLS THE UNTAGGED REFRESH, which is how this shipped and what an
    earlier version of this test could not see (it asserted only that the
    refresher lacked `never`). Measured with `--list-tasks --tags patch`: the
    holds, both fence passes and the upgrade were listed and the CACHE REFRESH
    WAS NOT. So on the single invocation that upgrades, the fence whose job is
    to refuse the upgrade on a drifted repo evaluated a cache nothing in the run
    had touched, and the cache was refreshed afterwards by the upgrade task
    itself. `--production --check --diff` had the same gap for a different
    reason: `ansible.builtin.apt` guards its refresh with
    `if not module.check_mode`.

    `always` specifically -- `patch` would still leave it skipped on any other
    tag-limited run, and the ordering guarantee has to hold on all of them.
    """
    refreshers = [
        t for t in COMMON()
        if _is(t, "apt") and _args(t).get("update_cache") and "never" not in _tags(t)
    ]
    assert refreshers, (
        "no unconditional `update_cache` task left in the common role; the "
        "track fence would read a stale apt cache"
    )
    untagged = [t.get("name") for t in refreshers if "always" not in _tags(t)]
    assert len(untagged) < len(refreshers), (
        "the apt cache refresh is not tagged `always`, so `--tags patch` -- the "
        "ONLY invocation that moves packages -- runs the repo-drift fence "
        f"against a stale cache: {untagged}"
    )
    # It must also still come before the fence it feeds, on the same run.
    tasks = COMMON()
    first_refresh = _index(
        tasks,
        lambda t: _is(t, "apt") and _args(t).get("update_cache") and "always" in _tags(t),
    )
    fences = _includes(tasks, "mysql_track_fence.yml")
    assert fences and first_refresh is not None
    assert first_refresh < min(fences), (
        "the apt cache is refreshed AFTER the repo-track fence reads it"
    )


def test_no_apt_task_allows_changing_held_packages():
    """`ansible.builtin.apt` has `allow_change_held_packages`, default false.

    Kills: the reflexive fix. The first person to meet `E: Held packages were
    changed` will be tempted to set this true on the mysql install task, which
    silently reopens every door this ticket closes -- the holds stay declared
    and stop doing anything.

    ⚠⚠ IT IS FENCED IN THREE SHAPES, NOT ONE. `module_defaults` is an ansible
    KEYWORD: `_module()` steps over it and a sweep of module arguments cannot
    see it, while it applies to every `apt` task in its scope. Measured green
    before this was widened -- adding

        module_defaults:
          ansible.builtin.apt: {allow_change_held_packages: true}

    at PLAY level in `site.yml` left the entire suite passing with the pin
    comprehensively defeated. Block-level and `roles: [{role: x,
    module_defaults: ...}]` are the same hole by a different route.
    """
    offenders = [
        f"{name}: {task.get('name')!r}"
        for name, tasks in _every_task_file().items()
        for task in tasks
        if _is(task, "apt") and _args(task).get("allow_change_held_packages")
    ]
    for where, defaults in _module_defaults():
        for module, args in defaults.items():
            if not isinstance(args, dict):
                continue
            if str(module).split(".")[-1] == "apt" and args.get(
                "allow_change_held_packages"
            ):
                offenders.append(f"{where} via module_defaults[{module}]")
    assert not offenders, (
        "allow_change_held_packages defeats the whole pin while leaving it "
        f"declared and looking green: {offenders}"
    )


def test_nothing_in_the_play_unholds_a_package_or_forces_apt():
    """Three more routes that leave the pin declared and inert, none of which
    was fenced at all.

    * `apt-mark unhold` / `apt-mark hold --remove` from a `command:` or
      `shell:`. MIGRATION.md's deliberate-move procedure runs this BY HAND, in a
      window, after a snapshot. In the play it is an automated unpin.
    * `dpkg_selections` with `selection: install` -- the exact inverse of the
      hold task, in the same module, one word different.
    * `apt: force: yes`, which passes `--force-yes` and overrides holds.

    None of these is a hypothetical: each is what a person reaches for after
    meeting `E: Unable to correct problems, you have held broken packages`,
    which the pin makes reachable on an ordinary MySQL patch release.
    """
    unhold = re.compile(r"\bapt-mark\b.*\bunhold\b|\bapt-mark\b.*--remove", re.S)
    offenders = []
    for name, tasks in _every_task_file().items():
        for task in tasks:
            where = f"{name}: {task.get('name')!r}"
            module = _module(task)
            raw = task.get(module) if module else None
            blob = (
                " ".join(str(v) for v in raw.values())
                if isinstance(raw, dict) else str(raw)
            )
            if _is(task, "command", "shell", "raw") and unhold.search(blob):
                offenders.append(f"{where} runs apt-mark unhold")
            if _is(task, "dpkg_selections") and str(
                _args(task).get("selection", "")
            ).lower() in ("install", "unhold", "deinstall", "purge"):
                offenders.append(
                    f"{where} sets dpkg selection "
                    f"{_args(task).get('selection')!r}, undoing the hold"
                )
            if _is(task, "apt") and str(_args(task).get("force", "")).lower() in (
                "yes", "true"
            ):
                offenders.append(f"{where} sets apt force:, which overrides holds")
    assert not offenders, (
        "something in the play removes or overrides the MySQL package holds. "
        "Unholding is a deliberate, windowed, snapshotted operator action "
        f"(infra/MIGRATION.md), never a task: {offenders}"
    )


# --- where the holds run ----------------------------------------------------


def _includes(tasks: list[dict], filename: str) -> list[int]:
    hits = []
    for i, task in enumerate(tasks):
        if not _is(task, "include_tasks"):
            continue
        args = task.get(_module(task))
        target = args.get("file") if isinstance(args, dict) else args
        if str(target).endswith(filename):
            hits.append(i)
    return hits


def test_the_holds_run_before_anything_that_can_move_a_package():
    """Kills: holds placed after the upgrade task, which protect nothing on the
    one run where a package can actually move."""
    tasks = COMMON()
    holds = _includes(tasks, "holds.yml")
    assert holds, "common/tasks/main.yml never includes holds.yml"
    upgrades = [i for i, t in enumerate(tasks) if _upgrades(t)]
    assert min(holds) < min(upgrades, default=len(tasks)), (
        "the package holds are applied AFTER an upgrade-capable task; on a "
        "`--tags patch` run the upgrade would happen unheld"
    )


def test_the_holds_and_the_fence_survive_a_tag_limited_run():
    """The upgrade task is reachable only via `--tags patch`, and that is the
    single invocation on which a package can move.

    Kills: holds and fence that carry no tag, so the run that can move a package
    is also the one that skips both. `apply:` matters as much as `tags:` --
    tagging only the include leaves the included tasks untagged.

    ⚠⚠ THE NON-EMPTINESS ASSERTS ARE THE POINT. Without them this was a nested
    `for` over a list that could legitimately be empty, i.e. STRUCTURALLY
    VACUOUS -- it passed against a tree with zero includes in it, which is
    exactly what `main` looked like before this ticket. A loop with no
    assertion that it looped is not a fence.

    ⚠ `run-playbook.sh`'s banner used to advertise `-- --tags mysql` and this
    docstring used to cite it. Measured with `--list-tasks --tags mysql`: there
    are no topic tags anywhere in this tree, so that invocation runs only the
    `always`-tagged tasks. The banner has been corrected; `--tags patch` is the
    real reason these tags exist.
    """
    tasks = COMMON()
    for filename in ("holds.yml", "mysql_track_fence.yml"):
        found = _includes(tasks, filename)
        assert found, f"common/tasks/main.yml never includes {filename} at all"
        checked = 0
        for i in found:
            task = tasks[i]
            if "never" in _tags(task):
                continue  # the deliberate post-upgrade re-run, `patch`-scoped
            checked += 1
            assert "always" in _tags(task), f"{filename} include is not tagged always"
            assert "always" in _applied_tags(task), (
                f"{filename} include does not `apply:` the always tag to the "
                "tasks it includes, so a tag-limited run skips them"
            )
        assert checked, (
            f"every {filename} include is `never`-tagged, so a default converge "
            "runs none of them"
        )


def _hold_reapplications() -> list[tuple[str, dict]]:
    """Every place that re-includes `common`'s holds after a package install."""
    out: list[tuple[str, dict]] = []
    site = SITE()
    for section in ("tasks", "post_tasks"):
        for task in _flatten(site.get(section)):
            out.append((f"site.yml {section}", task))
    for task in _flatten(_load(ANSIBLE() / "roles" / "mysql" / "tasks" / "main.yml")):
        out.append(("roles/mysql/tasks/main.yml", task))
    return [
        (where, t) for where, t in out
        if _is(t, "include_role", "import_role")
        and str(_args(t).get("tasks_from", "")).startswith("holds")
    ]


def test_the_holds_are_reapplied_after_mysql_is_installed():
    """Kills: a rebuilt host finishing the run unheld.

    On a fresh box the `common` pass resolves to an empty set (correctly --
    nothing is installed yet), then the mysql role installs the server. Without
    a second pass the host stays unpinned until somebody happens to converge it
    again.

    ⚠⚠ AND KILLS THE `post_tasks`-ONLY VERSION, which did not deliver the
    guarantee it claimed. `post_tasks` DO NOT RUN AFTER A ROLE FAILURE, and
    `site.yml:5-10` records the play's own documented failure mode -- the
    2026-08-18 scratch run where the redis role failed AFTER the mysql role had
    installed MySQL. That host finished unheld. The re-apply therefore has to
    sit in the mysql role, immediately after the install task, where nothing
    between "MySQL exists" and "MySQL is held" can fail.

    ⚠ AND KILLS THE UNTAGGED RE-APPLY. Measured green: dropping `tags:
    [always]` and `apply:` from the `site.yml` pass left the suite passing. That
    matters most on `--tags patch`, the one invocation that moves packages: the
    upgrade can itself pull in a MySQL package that was not on the box when the
    first hold pass ran, and an untagged re-apply is skipped on exactly that
    run.
    """
    found = _hold_reapplications()
    wheres = {where for where, _ in found}
    assert "roles/mysql/tasks/main.yml" in wheres, (
        "the mysql role does not re-apply common's holds after installing the "
        "server. site.yml's post_tasks are NOT a substitute: they do not run "
        "after a role failure, which is the play's own documented failure mode"
    )
    assert any(w.startswith("site.yml") for w in wheres), (
        "site.yml no longer re-applies the holds after every role; that pass is "
        "the backstop for a future role that installs a MySQL package"
    )
    for where, task in found:
        assert "always" in _tags(task), (
            f"{where}: the hold re-apply is not tagged `always`, so a "
            f"tag-limited run installs MySQL and skips the re-hold ({task.get('name')!r})"
        )
        assert "always" in _applied_tags(task), (
            f"{where}: the hold re-apply does not `apply: {{tags: [always]}}`, "
            "so the tag stops at the include and the included tasks are skipped"
        )

    # The mysql-role pass must come after the task that can install MySQL, or it
    # is holding a package that is not there yet.
    mysql_tasks = _flatten(_load(ANSIBLE() / "roles" / "mysql" / "tasks" / "main.yml"))
    install = _index(
        mysql_tasks,
        lambda t: _is(t, "apt") and "mysql_resolved_server_package" in str(_args(t)),
    )
    reapply = _index(
        mysql_tasks,
        lambda t: _is(t, "include_role", "import_role")
        and str(_args(t).get("tasks_from", "")).startswith("holds"),
    )
    assert install is not None and reapply is not None
    assert reapply > install, (
        "the mysql role re-applies the holds BEFORE it installs the server, so "
        "on a fresh box it holds nothing and the box finishes unheld"
    )


# --- how the hold set is derived -------------------------------------------


def _dpkg_task() -> dict:
    task = next((t for t in HOLDS() if _is(t, "dpkg_selections")), None)
    assert task is not None, "holds.yml has no dpkg_selections task"
    return task


def test_the_hold_loop_is_host_derived_and_does_not_swallow_errors():
    """Two wrong implementations, one fence.

    (i) A static `loop: "{{ mysql_hold_candidates }}"`. Measured in a real
    ubuntu:24.04 container: `dpkg_selections` HARD FAILS on a package the host
    does not have ("Failed to find package 'X' to perform selection 'hold'"),
    so a static list kills every scratch rehearsal and every fresh bootstrap,
    because production and scratch run different MySQL package families.

    (ii) The obvious fix for (i) -- bolting on `ignore_errors: true` -- which
    converts the hard failure into a pin that silently holds nothing.

    ⚠⚠ BOTH OF THESE USED TO SURVIVE THIS TEST, despite being named in this
    very docstring:

    * (ii) via a `block:` carrying `ignore_errors: true` (or a `rescue:`).
      `_flatten` pulled the child up and the `"ignore_errors" not in task` check
      then inspected a dict with no such key. `_flatten` now threads ancestor
      error handling down; `test_this_module_sees_through_a_block_that_swallows_errors`
      pins that helper.
    * (i) via `mysql_resolved_holds: "{{ mysql_hold_candidates }}"` with the
      `intersect` deleted -- reintroducing M1's hard failure on every scratch
      rehearsal and every fresh bootstrap. The check was
      `"ansible_facts.packages" in str(<the whole set_fact args dict>)`, and the
      two SIBLING keys in that same dict mention `ansible_facts.packages`, so it
      passed for free. It now reads the `mysql_resolved_holds` VALUE.
    """
    task = _dpkg_task()
    loop = str(task.get("loop", ""))
    assert "mysql_resolved_holds" in loop, (
        f"the hold loop is not the host-derived intersection; it loops over {loop!r}"
    )
    swallowed = _swallows_errors(task)
    assert not swallowed, (
        "the hold task's failure would not fail the play, so a pin that holds "
        f"nothing reports success. Reason(s): {swallowed}"
    )
    resolver = next(
        (t for t in HOLDS() if _is(t, "set_fact")
         and "mysql_resolved_holds" in _args(t)),
        None,
    )
    assert resolver is not None, "nothing sets mysql_resolved_holds"
    holds_expression = str(_args(resolver)["mysql_resolved_holds"])
    assert "ansible_facts.packages" in holds_expression, (
        "mysql_resolved_holds is not derived from installed packages, so it can "
        f"name a package the host lacks and hard-fail the play. Got: "
        f"{holds_expression!r}"
    )
    assert any(_is(t, "package_facts") for t in HOLDS()), (
        "nothing gathers package_facts, so ansible_facts.packages is undefined"
    )


def test_the_server_package_resolution_preserves_detect_order():
    """Kills a NONDETERMINISTIC pin that no other fence here can see.

    `mysql_resolved_server_package` reads a POSITIONAL element (`| first`) off
    the detect order. ansible-core implements `intersect` as
    `list(set(a) & set(b))` (`ansible/plugins/filter/mathstuff.py`), and set
    iteration order over strings depends on PYTHONHASHSEED, which is randomised
    per process. MEASURED against ansible-core 2.21.3, six runs, identical
    inputs: `mysql_server_detect_order | intersect([...]) | first` returned
    `mysql-server` three times and `mysql-server-8.0` three times.

    That is a coin flip on every converge of any host with more than one
    detect-order name installed -- i.e. every stock-Ubuntu host, since
    `apt install mysql-server` on noble installs `mysql-server` AND
    `mysql-server-8.0`. It decides which package the mysql role installs, which
    package the track fence probes with `apt-cache policy`, and what the
    anti-vacuity assert compares against.

    ⚠ It also made `test_the_hold_candidate_list_covers_both_mysql_package_families`
    VACUOUS: that test asserts `detect[0] == "mysql-community-server"` because
    "detection must prefer the Oracle package", certifying an ordering the
    runtime discarded.

    ⚠ `mysql_resolved_holds` and `mysql_installed_server_present` are
    deliberately NOT covered here: one feeds a loop and one feeds
    `| length > 0`, so neither reads a positional element and `intersect` is
    correct in both.
    """
    resolver = next(
        (t for t in HOLDS() if _is(t, "set_fact")
         and "mysql_resolved_server_package" in _args(t)),
        None,
    )
    assert resolver is not None, "nothing sets mysql_resolved_server_package"
    expression = " ".join(str(_args(resolver)["mysql_resolved_server_package"]).split())
    assert "intersect" not in expression, (
        "mysql_resolved_server_package is resolved with `intersect`, which "
        "ansible implements as list(set(a) & set(b)) -- so `| first` picks a "
        "PYTHONHASHSEED-dependent element and the resolved package flips "
        "between converges of an unchanged host. Filter the detect order in "
        "place instead (`select('in', ...) | list | first`). Got: "
        f"{expression!r}"
    )
    assert "mysql_server_detect_order" in expression, (
        "the server package is not resolved from the declared detect order"
    )
    assert "ansible_facts.packages" in expression, (
        "the server package is not resolved against what is actually installed"
    )
    assert "default(mysql_server_package" in expression.replace(" ", ""), (
        "no fallback for a host with no MySQL server installed yet, so a fresh "
        "bootstrap resolves to Undefined"
    )


def test_the_hold_candidate_list_covers_both_mysql_package_families():
    """Kills: a list written from the mysql role's existing `mysql-server` line.

    Production runs Oracle's `mysql-community-server` after the TBD-360
    cutover; a scratch droplet gets Ubuntu's `mysql-server`. A list naming only
    one family intersects to almost nothing on the other host, the play stays
    green, and the pin is decoration. `mysql-apt-config` is the package the
    2026-08-19 near miss was actually about -- it is the repo SELECTOR.
    """
    candidates = DEFAULTS()["mysql_hold_candidates"]
    for required in ("mysql-apt-config", "mysql-community-server", "mysql-server"):
        assert required in candidates, f"{required} is not in mysql_hold_candidates"
    detect = DEFAULTS()["mysql_server_detect_order"]
    assert detect[0] == "mysql-community-server", (
        "detection must prefer the Oracle package; production runs it and a "
        "generic name would resolve to the wrong package there"
    )
    assert set(detect) <= set(candidates), (
        f"these can be detected as the server but never held: "
        f"{sorted(set(detect) - set(candidates))}"
    )


def test_redis_is_deliberately_not_held():
    """Kills: the well-meaning "pin the whole data plane" edit.

    Ubuntu noble ships exactly one Redis major and the security pocket only ever
    ships 7.0.x for it, so there is no track to jump -- a Redis hold prevents
    nothing this ticket is about. It costs real currency: it blocks
    unattended-upgrades from ever patching a VPC-facing service, permanently,
    with nothing in the repo to notice. The MySQL holds are free by comparison,
    because Oracle's `MySQL` origin is not in unattended-upgrades' allowlist.

    ⚠⚠ THE SWEEP OVER TASK BODIES IS NOT DECORATION. Checking only
    `mysql_hold_candidates` measured green against the obvious edit: a
    `dpkg_selections: {name: redis-server, selection: hold}` added straight to
    the redis role never touches that list, and the redis role is where somebody
    who wants to pin Redis would naturally put it.
    """
    held = " ".join(DEFAULTS()["mysql_hold_candidates"])
    assert "redis" not in held and "valkey" not in held, (
        "a Redis package is in the hold set; that permanently blocks security "
        "patching on the auth-session store to prevent a major jump that "
        "cannot happen. Restart policy is a separate ticket."
    )
    offenders = []
    for name, tasks in _every_task_file().items():
        for task in tasks:
            if not _is(task, "dpkg_selections"):
                continue
            blob = f"{_args(task)} {task.get('loop', '')} {task.get('vars', '')}"
            if "redis" in blob or "valkey" in blob:
                offenders.append(f"{name}: {task.get('name')!r}")
    assert not offenders, (
        "a task holds a Redis package directly, bypassing "
        f"mysql_hold_candidates entirely: {offenders}"
    )


def test_the_pin_refuses_to_resolve_to_nothing():
    """Kills THE vacuous pin: wrong package names -> empty intersection -> the
    loop runs zero times -> green forever with production entirely unpinned.

    Also requires the read-back, because `dpkg_selections` is idempotent and
    `mysql-apt-config` was already held by hand on 2026-08-19 -- so the first
    production converge reports `ok` and proves nothing on its own.
    """
    asserts = [t for t in HOLDS() if _is(t, "assert")]
    thats = " ".join(str(_args(t).get("that")) for t in asserts)
    assert "mysql_resolved_server_package in mysql_resolved_holds" in thats.replace(
        "'", ""
    ), "nothing asserts that the resolved server package is actually held"

    vacuity = next(
        (t for t in asserts if "mysql_resolved_server_package" in str(t)), None
    )
    # ⚠ THE SECOND CLAUSE IS FENCED SEPARATELY, and was green when deleted.
    # `mysql-apt-config` is the repo SELECTOR -- the package the 2026-08-19 near
    # miss was actually about, and the one whose postinst decides which MySQL
    # major track apt offers. A host can have it installed and NOT held while
    # the server package is held, which is a pin that leaves the drift door
    # open; the first clause alone cannot see that.
    vacuity_clauses = " ".join(str(c) for c in (_args(vacuity or {}).get("that") or []))
    assert "mysql-apt-config" in vacuity_clauses, (
        "the anti-vacuity assert no longer requires an INSTALLED mysql-apt-config "
        "to be held. That is the repo selector: unheld, its postinst can "
        "re-point the MySQL track underneath a pinned server"
    )
    assert "not in ansible_facts.packages" in vacuity_clauses, (
        "the mysql-apt-config clause is not conditioned on the package actually "
        "being installed, so it reds every Ubuntu-family host, which does not "
        "have it -- and the fix for that is always to delete the clause"
    )
    assert vacuity is not None and "mysql_installed_server_present" in str(
        vacuity.get("when", "")
    ), (
        "the anti-vacuity assert is not gated on a server actually being "
        "installed, so it reds a legitimately fresh bootstrap -- and the fix "
        "for that is always to delete it"
    )
    readback = next(
        (t for t in HOLDS() if _is(t, "command")
         and "get-selections" in str(t.get(_module(t)))),
        None,
    )
    assert readback is not None, (
        "nothing reads the dpkg selections back, so the only evidence the pin "
        "is in place is the module's own idempotent `ok`"
    )

    # ⚠⚠ THE READ-BACK'S ASSERT IS FENCED HERE, AND WAS NOT FENCED AT ALL.
    # Measured green: deleting the whole `Assert every resolved package is
    # actually held on the box` task while KEEPING the `command:` left the suite
    # passing -- the read-back degraded to a `register:` nobody reads, pure
    # decoration. The spec calls that assert the only thing that turns "the
    # module said so" into "the dpkg database says so", which it has to be
    # because M2 makes the first production converge report `ok` and prove
    # nothing on its own.
    register = readback.get("register")
    assert register, "the dpkg read-back registers nothing, so nothing can read it"
    consumers = [
        t for t in asserts
        if register in str(_args(t).get("that"))
        and "mysql_resolved_holds" in str(_args(t).get("that"))
    ]
    assert consumers, (
        f"nothing ASSERTS on {register!r}: the dpkg read-back is registered and "
        "never read, so the pin's only evidence is the module's own idempotent "
        "`ok` again. The assert must compare mysql_resolved_holds against what "
        "dpkg reports as held"
    )
    readback_assert = consumers[0]
    clauses = " ".join(str(c) for c in (_args(readback_assert).get("that") or []))
    assert "hold" in clauses, (
        "the read-back assert does not filter the selections for `hold`, so it "
        "passes on a package dpkg reports as `install`"
    )
    assert "not ansible_check_mode" in str(readback_assert.get("when", "")), (
        "the read-back ASSERT is not gated on check mode; --check converges "
        "nothing, so a dry run of a healthy box would fail claiming the pin is "
        "broken (M3: dpkg_selections reports `changed` and writes nothing)"
    )
    # ⚠ The read-back and the track fence are gated OPPOSITELY, on purpose, and
    # getting this backwards is a FALSE RED on a healthy box rather than a
    # missed defect. `dpkg_selections` declares full check-mode support: under
    # --check it reports `changed` and writes nothing, so an ungated read-back
    # correctly observes "not held" and fails the documented
    # `--production --check --diff` pre-flight. Measured against a real
    # ubuntu:24.04. The track fence reads REPO state, which --check does not
    # affect, so it is ungated. Do not harmonise them.
    assert "not ansible_check_mode" in str(readback.get("when", "")), (
        "the dpkg read-back is not gated on check mode; it asserts CONVERGED "
        "state and --check converges nothing, so a dry run of a healthy box "
        "would fail claiming the pin is broken"
    )
    assert "ansible_check_mode" not in str(vacuity.get("when", "")), (
        "the anti-vacuity assert IS gated on check mode; it compares two "
        "package_facts-derived lists, which --check computes fine, and a dry "
        "run is exactly where wrong package names should surface"
    )


# --- the track fence --------------------------------------------------------


def _fence_read() -> dict:
    task = next((t for t in FENCE() if _is(t, "command")), None)
    assert task is not None, "the track fence has no apt-cache read"
    return task


def _fence_assert() -> dict:
    task = next((t for t in FENCE() if _is(t, "assert")), None)
    assert task is not None, "the track fence has no assert"
    return task


def test_the_track_fence_uses_the_parser_and_carries_no_version_literal():
    """Two wrong implementations.

    (i) `'8.4' in policy.stdout`. `apt-cache policy` prints the whole version
    table, so 8.4.11 is still in stdout on a host whose Candidate is 9.0.1 --
    the substring passes on exactly the drift being hunted. Requiring the
    filters is what routes the parse to test_apt_policy_filter.py, which CAN
    see that.

    (ii) A hardcoded `8.4`. It reds every scratch rehearsal against stock
    Ubuntu, and the reflexive fix is to widen it to `8.` -- one character from
    permitting 9.0 -- or to delete the fence. It would also have to be edited
    mid-window at the next legitimate major move, which is the classic moment a
    fence gets defanged. The invariant is host-derived: candidate track ==
    installed track.

    Scope is `that:` and `vars:` only. The `fail_msg` may name versions; it is
    prose for a human, not the assertion.

    ⚠⚠ (iii) THE PLAUSIBLE HALF-FIX, WHICH USED TO PASS: deleting the actual
    comparison out of `that:`. This test only ever checked that the strings
    `apt_policy_candidate` and `apt_version_track` appeared SOMEWHERE in
    `that:` + `vars:` -- and both live in `vars:`, so the entire `that:` block
    could be gutted with the filters left correctly wired and the suite stayed
    green. That is precisely what the first person to hit a false red on a
    scratch box does. The comparison itself is now asserted.

    ⚠ The version-literal ban is SCOPED so a package name cannot trip it.
    `mysql-server-8.0` is in `mysql_hold_candidates` and is a perfectly correct
    thing for a clause to name, but `\\b\\d+\\.\\d+\\b` matched the `8.0` inside
    it and would have gone red on a correct edit. Identifier-shaped tokens are
    scrubbed first; a bare `8.4` does not start with a letter and survives.
    """
    fence = _fence_assert()
    body = _args(fence)
    clauses = [" ".join(str(c).split()) for c in (body.get("that") or [])]
    joined = " ".join(clauses)
    expressions = f"{body.get('that')} {fence.get('vars')}"
    variables = {k: " ".join(str(v).split()) for k, v in (fence.get("vars") or {}).items()}

    assert "apt_policy_candidate" in expressions, "the fence does not use the parser"
    assert "apt_version_track" in expressions, "the fence does not compare tracks"

    # -- (iii) the comparison must be IN `that:`, not merely implied by `vars:`.
    def _side(clause: str, want: str) -> bool:
        """Does one side of this `==` resolve through `want`'s filter chain?"""
        return any(
            want in variables.get(token, "") or want in token
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", clause)
        )

    comparisons = [
        c for c in clauses
        if "==" in c
        and _side(c.split("==")[0], "apt_policy_candidate")
        and _side(c.split("==")[1], "apt_policy_installed")
    ] + [
        c for c in clauses
        if "==" in c
        and _side(c.split("==")[0], "apt_policy_installed")
        and _side(c.split("==")[1], "apt_policy_candidate")
    ]
    assert comparisons, (
        "the track fence's `that:` block does not actually compare the "
        "CANDIDATE track against the INSTALLED track. Wiring the filters up in "
        f"`vars:` and asserting nothing with them is not a fence. that: {clauses}"
    )

    # -- (iv) both fail-closed length guards, which are M4's whole point.
    for which in ("candidate", "installed"):
        guarded = [
            c for c in clauses
            if "length" in c and ">" in c
            and _side(c.split("length")[0], f"apt_policy_{which}")
        ]
        assert guarded, (
            f"the fence has no `| length > 0` guard on the {which} track. "
            "M4: `apt-cache policy` on an unreadable package exits 0 with EMPTY "
            "stdout, and `apt-get update` exits 0 against a repo whose signature "
            "failed -- so without this the fence compares '' to '' and PASSES on "
            f"exactly the state it exists to catch. that: {clauses}"
        )

    scrubbed = re.sub(r"[A-Za-z_][A-Za-z0-9_.+-]*", " ", expressions)
    literals = re.findall(r"\b\d+\.\d+\b", scrubbed)
    assert not literals, (
        f"the track fence hardcodes version literal(s) {literals}; the "
        "invariant must be derived from the host (candidate track == installed "
        "track), or it reds every scratch rehearsal and goes stale mid-window"
    )
    assert joined, "the fence's `that:` block is empty"


def test_the_track_fence_runs_under_check_mode():
    """The highest-value fence in the set, because the wrong implementation is
    the one the surrounding code teaches.

    The fences at the bottom of the mysql and redis roles are gated on
    `not ansible_check_mode` because they assert properties of the CONVERGED
    server. This one asserts a property of the apt REPOSITORY, which is
    identical before and after the play -- and `--production --check --diff` is
    the documented pre-flight, i.e. the single run where finding repo drift
    matters most. Copying the neighbouring gate silently removes the fence from
    the only place it would ever have fired in time.
    """
    read = _fence_read()
    assert read.get("check_mode") is False, (
        "the apt-cache read has no `check_mode: false`, so under --check the "
        "command module self-reports skipped with stdout '' and the fence is "
        "either vacuous or falsely red"
    )
    assert read.get("changed_when") is False, (
        "a read-only probe reporting `changed` pollutes the play recap the "
        "operator reads to decide whether anything happened"
    )
    for task in FENCE():
        assert "ansible_check_mode" not in str(task.get("when", "")), (
            f"{task.get('name')!r} gates the repo-state fence on check mode; "
            "that is the convergence-fence pattern and it does not apply here"
        )


def test_the_track_fence_reruns_after_a_deliberate_upgrade():
    """Kills: a `--tags patch` run that moves `mysql-apt-config`, whose postinst
    rewrites /etc/apt/sources.list.d/mysql.list from its stored debconf
    selections -- so the repo the fence approved at the top of the play is not
    the repo in force at the bottom of it. This is the only check on the one
    moment the pin is deliberately lifted.
    """
    tasks = COMMON()
    fences = _includes(tasks, "mysql_track_fence.yml")
    upgrades = [i for i, t in enumerate(tasks) if _upgrades(t)]
    assert upgrades, "no upgrade task at all; this fence's premise is gone"
    assert any(i < min(upgrades) for i in fences), "the fence never runs before the upgrade"
    assert any(i > max(upgrades) for i in fences), (
        "the track fence never re-runs after the upgrade task, so a patch run "
        "that re-points the MySQL repo goes unnoticed until the next converge"
    )


def test_the_mysql_install_targets_the_resolved_server_package():
    """Kills a defect the pin would otherwise CREATE.

    `mysql-community-server` Provides only `virtual-mysql-server`, not
    `mysql-server` (measured against repo.mysql.com's noble index). So on
    production `apt: name=mysql-server state=present` is not satisfied by the
    installed server; once `mysql-community-server` is held at 8.4.11 that task
    resolves `mysql-server 8.4.12`, whose `Depends: mysql-community-server
    (= 8.4.12)` cannot be met, and apt exits 100 with "you have held broken
    packages" -- on an ORDINARY patch release, with a message naming the hold
    rather than the cause.

    ⚠ LOCATED BY WHAT IT INSTALLS, NOT BY A CO-INSTALLED PACKAGE. This used to
    find the task with `"python3-pymysql" in name`, which would have gone RED on
    a perfectly correct edit: the `common` role's baseline task installs
    `python3-pymysql` too, so deduping it out of the mysql role is a legitimate
    change that made the fence unable to find the task at all. The property is
    stated directly instead -- NO apt task in this role may name a MySQL server
    package literally, and at least one must resolve it -- which is also
    stronger, because it covers a second apt task added later.
    """
    tasks = _flatten(_load(ANSIBLE() / "roles" / "mysql" / "tasks" / "main.yml"))
    apt_tasks = [t for t in tasks if _is(t, "apt")]
    assert apt_tasks, "the mysql role has no apt task at all"

    literal = [
        f"{t.get('name')!r} -> {_args(t).get('name')!r}"
        for t in apt_tasks
        if any(
            pkg in str(_args(t).get("name"))
            for pkg in ("mysql-server", "mysql-community-server")
        )
    ]
    assert not literal, (
        "the mysql role installs a hardcoded MySQL server package name; on "
        "production that is not the package that is installed and held, and "
        "apt exits 100 with 'you have held broken packages' on an ordinary "
        f"patch release: {literal}"
    )
    resolved = [t for t in apt_tasks if "mysql_resolved_server_package" in str(_args(t).get("name"))]
    assert resolved, (
        "no apt task in the mysql role installs mysql_resolved_server_package, "
        "so nothing guarantees the server this play manages is the server that "
        "is actually on the box"
    )


def test_the_filter_plugin_is_discoverable_by_ansible():
    """Kills: the plugin landing somewhere ansible never looks.

    `filter_plugins/` is discovered next to the PLAYBOOK (here
    `playbooks/site.yml`), not at the ansible.cfg root, so an
    `infra/ansible/filter_plugins/` directory does not auto-load. Without the
    config key the fence's template raises at run time, on the production data
    droplet, at the top of a maintenance window.
    """
    cfg = configparser.ConfigParser()
    cfg.read(ANSIBLE() / "ansible.cfg")
    configured = cfg.get("defaults", "filter_plugins", fallback=None)
    assert configured, "ansible.cfg [defaults] has no filter_plugins path"
    resolved = (ANSIBLE() / configured.strip()).resolve()
    assert (resolved / "apt_policy.py").exists(), (
        f"filter_plugins points at {resolved}, which has no apt_policy.py"
    )
