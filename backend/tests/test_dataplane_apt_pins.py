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


# Task-level keywords, so whatever key is left over is the module being called.
KEYWORDS = {
    "name", "when", "notify", "register", "changed_when", "failed_when", "loop",
    "with_items", "with_dict", "loop_control", "become", "become_user", "tags",
    "vars", "args", "ignore_errors", "no_log", "block", "rescue", "always",
    "check_mode", "delegate_to", "run_once", "environment", "until", "retries",
    "delay", "listen", "any_errors_fatal", "throttle", "diff", "connection",
    "remote_user", "module_defaults", "collections", "poll", "async",
    "delegate_facts", "ignore_unreachable",
}


def _load(path: pathlib.Path):
    return yaml.safe_load(path.read_text()) or []


def _flatten(tasks) -> list[dict]:
    """Task list with `block:`/`rescue:`/`always:` children pulled up.

    ⚠ Without this, wrapping a re-added `upgrade: safe` in a `block:` would hide
    it from every fence below while changing nothing about what runs.
    """
    out: list[dict] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for section in ("block", "rescue", "always"):
            if isinstance(task.get(section), list):
                out.extend(_flatten(task[section]))
    return out


def _module(task: dict) -> str | None:
    for key in task:
        if key not in KEYWORDS:
            return key
    return None


def _is(task: dict, *names: str) -> bool:
    module = _module(task)
    return module is not None and module.split(".")[-1] in names


def _tags(task: dict) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _applied_tags(task: dict) -> set[str]:
    """Tags an `include_tasks` pushes down onto the tasks it includes."""
    module = task.get("ansible.builtin.include_tasks") or task.get("include_tasks") or {}
    if not isinstance(module, dict):
        return set()
    tags = (module.get("apply") or {}).get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _every_task_file() -> dict[str, list[dict]]:
    files = {}
    for path in sorted(ANSIBLE().glob("roles/*/tasks/*.yml")):
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


def _upgrades(task: dict) -> bool:
    if not _is(task, "apt"):
        return False
    args = task.get(_module(task)) or {}
    if not isinstance(args, dict):
        return False
    return str(args.get("upgrade", "no")).lower() not in ("no", "false", "none")


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


# --- the upgrade task -------------------------------------------------------


def test_no_apt_task_upgrades_packages_outside_the_never_tag():
    """THE fence. Sweeps the WHOLE ansible tree, not just `common`, so adding a
    new role with an unconditional `upgrade:` is caught too.

    Kills: reverting to `upgrade: safe` on a task that runs by default, which is
    the literal defect this ticket exists to close. Also kills the half-fix of
    gating it on a variable -- a variable is settable from role defaults,
    group_vars, inventory.yml or run-playbook.sh's generated extra-vars file,
    none of which the operator sees. Only `never` requires typing it.
    """
    offenders = [
        f"{name}: {task.get('name')!r} tags={sorted(_tags(task))}"
        for name, tasks in _every_task_file().items()
        for task in tasks
        if _upgrades(task) and "never" not in _tags(task)
    ]
    assert not offenders, (
        "an apt task upgrades packages without the `never` tag, so a routine "
        "converge of the production database droplet performs an unbounded "
        f"package upgrade again (TBD-419): {offenders}"
    )


def test_the_apt_cache_is_still_refreshed_on_a_routine_converge():
    """Kills: deleting the whole first task instead of only its `upgrade:` key.

    Silent and plausible. `Install baseline packages` and the track fence would
    then read whatever cache the DO image happened to ship with, so the fence
    would be answering a question about last week's repository.
    """
    refreshers = [
        t for t in COMMON()
        if _is(t, "apt")
        and (t.get(_module(t)) or {}).get("update_cache")
        and "never" not in _tags(t)
    ]
    assert refreshers, (
        "no unconditional `update_cache` task left in the common role; the "
        "track fence would read a stale apt cache"
    )


def test_no_apt_task_allows_changing_held_packages():
    """`ansible.builtin.apt` has `allow_change_held_packages`, default false.

    Kills: the reflexive fix. The first person to meet `E: Held packages were
    changed` will be tempted to set this true on the mysql install task, which
    silently reopens every door this ticket closes -- the holds stay declared
    and stop doing anything.
    """
    offenders = [
        f"{name}: {task.get('name')!r}"
        for name, tasks in _every_task_file().items()
        for task in tasks
        if _is(task, "apt")
        and (task.get(_module(task)) or {}).get("allow_change_held_packages")
    ]
    assert not offenders, (
        "allow_change_held_packages defeats the whole pin while leaving it "
        f"declared and looking green: {offenders}"
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
    """`run-playbook.sh`'s own usage banner documents `-- --tags mysql`, and the
    upgrade task is now reachable only via `--tags patch`.

    Kills: holds and fence that carry no tag, so the single invocation that can
    move a package is also the one that skips both. `apply:` matters as much as
    `tags:` -- tagging only the include leaves the included tasks untagged.
    """
    tasks = COMMON()
    for filename in ("holds.yml", "mysql_track_fence.yml"):
        for i in _includes(tasks, filename):
            task = tasks[i]
            if "never" in _tags(task):
                continue  # the deliberate post-upgrade re-run, `patch`-scoped
            assert "always" in _tags(task), f"{filename} include is not tagged always"
            assert "always" in _applied_tags(task), (
                f"{filename} include does not `apply:` the always tag to the "
                "tasks it includes, so a tag-limited run skips them"
            )


def test_the_holds_are_reapplied_after_the_roles_have_installed_packages():
    """Kills: a rebuilt host finishing the run unheld.

    On a fresh box the `common` pass resolves to an empty set (correctly --
    nothing is installed yet), then the mysql role installs the server. Without
    a second pass the host stays unpinned until somebody happens to converge it
    again.
    """
    post = SITE().get("post_tasks") or []
    includes = [
        t for t in _flatten(post)
        if _is(t, "include_role")
        and str((t.get(_module(t)) or {}).get("tasks_from", "")).startswith("holds")
    ]
    assert includes, (
        "site.yml has no post_tasks re-applying common's holds; a host whose "
        "MySQL was installed by THIS run ends the run unheld"
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
    """
    task = _dpkg_task()
    loop = str(task.get("loop", ""))
    assert "mysql_resolved_holds" in loop, (
        f"the hold loop is not the host-derived intersection; it loops over {loop!r}"
    )
    assert "ignore_errors" not in task and "failed_when" not in task, (
        "the hold task swallows its own errors, so a pin that holds nothing "
        "reports success"
    )
    resolver = next(
        (t for t in HOLDS() if _is(t, "set_fact")
         and "mysql_resolved_holds" in (t.get(_module(t)) or {})),
        None,
    )
    assert resolver is not None, "nothing sets mysql_resolved_holds"
    assert "ansible_facts.packages" in str(resolver[_module(resolver)]), (
        "mysql_resolved_holds is not derived from installed packages, so it can "
        "name a package the host lacks and hard-fail the play"
    )
    assert any(_is(t, "package_facts") for t in HOLDS()), (
        "nothing gathers package_facts, so ansible_facts.packages is undefined"
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
    """
    held = " ".join(DEFAULTS()["mysql_hold_candidates"])
    assert "redis" not in held and "valkey" not in held, (
        "a Redis package is in the hold set; that permanently blocks security "
        "patching on the auth-session store to prevent a major jump that "
        "cannot happen. Restart policy is a separate ticket."
    )


def test_the_pin_refuses_to_resolve_to_nothing():
    """Kills THE vacuous pin: wrong package names -> empty intersection -> the
    loop runs zero times -> green forever with production entirely unpinned.

    Also requires the read-back, because `dpkg_selections` is idempotent and
    `mysql-apt-config` was already held by hand on 2026-08-19 -- so the first
    production converge reports `ok` and proves nothing on its own.
    """
    asserts = [t for t in HOLDS() if _is(t, "assert")]
    thats = " ".join(str((t.get(_module(t)) or {}).get("that")) for t in asserts)
    assert "mysql_resolved_server_package in mysql_resolved_holds" in thats.replace(
        "'", ""
    ), "nothing asserts that the resolved server package is actually held"

    vacuity = next(
        (t for t in asserts if "mysql_resolved_server_package" in str(t)), None
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
    """
    import re

    body = _fence_assert()[_module(_fence_assert())]
    expressions = f"{body.get('that')} {_fence_assert().get('vars')}"
    assert "apt_policy_candidate" in expressions or "apt_policy_candidate" in str(
        _fence_assert().get("vars")
    ), "the fence does not use the apt-policy parser"
    assert "apt_version_track" in expressions, "the fence does not compare tracks"
    literals = re.findall(r"\b\d+\.\d+\b", expressions)
    assert not literals, (
        f"the track fence hardcodes version literal(s) {literals}; the "
        "invariant must be derived from the host (candidate track == installed "
        "track), or it reds every scratch rehearsal and goes stale mid-window"
    )


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
    """
    tasks = _flatten(_load(ANSIBLE() / "roles" / "mysql" / "tasks" / "main.yml"))
    install = next(
        (t for t in tasks if _is(t, "apt")
         and "python3-pymysql" in str((t.get(_module(t)) or {}).get("name"))),
        None,
    )
    assert install is not None, "could not find the mysql role's install task"
    names = str((install[_module(install)] or {}).get("name"))
    assert "mysql_resolved_server_package" in names, (
        "the mysql role installs a hardcoded server package name; on production "
        f"that is not the package that is installed and held. Got: {names!r}"
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
