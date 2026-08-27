"""Fence: every ansible task that handles a data-plane credential sets no_log (TBD-414).

⚠⚠ EVERY ASSERTION HERE PARSES THE YAML. None of them greps. The roles carry
long comment blocks that quote the very variable names being fenced -- the redis
role and its handlers both explain *why* a task deliberately omits ``no_log``
and name ``redis_password`` while doing so. A whole-file grep is satisfied by
the comment documenting its own exception. ``yaml.safe_load`` drops comments, so
parsing is the only form of this check that means anything, and
``test_a_secret_in_a_comment_is_ignored_but_one_in_a_value_is_not`` asserts that
property in both directions rather than claiming it.

## The hazard

On 2026-08-19 the plaintext ``mysql_app_password`` and ``redis_password`` were
pasted into an agent transcript while debugging, and had to be rotated on
principle. They became quotable by two routes:

* ``--check --diff`` prints a template's RENDERED CONTENT, and two templates
  render credentials -- ``root.my.cnf.j2`` (``mysql_backup_password``) and
  ``00-static.conf.j2`` (``requirepass {{ redis_password }}``).
* The three ``mysql_user`` tasks pass the password as cleartext
  ``plugin_auth_string``, which Ansible echoes on failure and under ``-vvv``.

## What this can and cannot see

It asserts that a task WHICH REFERENCES a secret variable by name carries
``no_log`` (its own, or inherited from an enclosing block). It cannot see a
credential that reaches the box without naming one of these variables: a
hardcoded literal, a value assembled from parts, or -- the realistic one -- an
ALIAS (``set_fact: pw: "{{ redis_password }}"`` then ``{{ pw }}``; the
``set_fact`` is caught, the later use is not). Nor can it see the indirect
reference forms ``{{ vars['redis_password'] }}`` or
``{{ hostvars[...].redis_password }}``, nor BARE-JINJA references -- ``when:``
and ``assert.that`` take unbraced expressions, so the fail-closed
``mysql_app_password != 'CHANGE_ME'`` assert is not detected (correctly: it
prints the condition source, not the value, and no_log there would be actively
harmful).

Read a green run as "no task naming a known secret variable prints it", not as
"no credential can be printed".

⚠ ``no_log: "true"`` as a quoted string is rejected. That is deliberate -- a
stringy or templated ``no_log`` is its own hazard -- but it is a red against
something a maintainer may consider correct.
"""

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
        "infra/ansible/playbooks/site.yml not found from a CI checkout; this "
        "fence must not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None, reason="ansible tree not present in this checkout"
)

# The credentials that are Terraform-generated, live in TFC state, and land in
# DATABASE_URL / REDIS_URL. `mysql_app_user` and friends are NOT secrets.
SECRET_VARS = frozenset(
    {"mysql_app_password", "mysql_backup_password", "redis_password"}
)

_SECRET_REF = re.compile(r"\{\{-?\s*(?:" + "|".join(sorted(SECRET_VARS)) + r")\b")

# ⚠ NOT a hardcoded role list, and NOT `tasks/main.yml` only. Both were in the
# first version of this file and both were wrong -- in ways
# `test_dataplane_apt_pins.py` had ALREADY measured and fixed, while this file
# cited it as its model. Its docstring says it plainly:
#
#   "ROLE HANDLERS ARE IN HERE, and leaving them out was a live gap: measured
#    green, an `apt: {upgrade: dist}` added to roles/common/handlers/main.yml
#    passed the whole suite, because the glob was roles/*/tasks/*.yml"
#
# The same omission here was also a LIVE gap, found by review rather than by
# this fence: `roles/redis/handlers/main.yml`'s "Apply redis live tunables"
# references `{{ redis_password }}` and the scan never opened the file. A
# handler is a task; it runs on the same box with the same privileges whenever
# something notifies it.
#
# An inclusion list of roles is additionally circular: the coverage test can
# only find tasks in roles the scan was told to look at, so a future role would
# be both invisible and unnoticeable. Globbing removes the question.
TASK_FILE_PATTERNS = (
    "roles/*/tasks/*.yml",
    "roles/*/handlers/*.yml",
    "playbooks/*.yml",
)

# ⚠ Tasks that reference a secret and deliberately do NOT set no_log. The
# reason is not decoration: `test_allowlisted_tasks_still_earn_their_exemption`
# asserts the SHAPE that earns it, because keying on the name alone fences the
# entry's existence and not its justification.
# ⚠⚠ THE REASON THESE TWO ARE EXEMPT IS *NOT* "the credential is safe here".
#
# The role comments claim `environment:` keeps the password out of argv and
# therefore away from `ps`. **That is false**, and it was inherited into the
# first version of this file as though verified. Measured against ansible-core
# 2.21.3: `environment:` is not passed to the module out of band -- it is
# rendered by `ShellBase.env_prefix` and PREPENDED TO THE MODULE COMMAND, so the
# target executes
#
#     /bin/sh -c 'REDISCLI_AUTH=<the password> /usr/bin/python AnsiballZ_*.py'
#
# The credential is in that argv for the whole task, which is a LONGER window
# than `redis-cli -a` would have been. And `display.vvv("SSH: EXEC ...")` is not
# censored by no_log -- only the result dict is -- so `-vvv` prints it.
#
# So `no_log: true` would NOT close these tasks' exposure. The honest reason it
# is off is narrower: their own stdout/stderr carry no credential, and that
# stderr is the only Redis-auth diagnostic there is. Censoring it would cost the
# signal TBD-412 needed and buy nothing.
#
# The argv exposure is real, is NOT closed by this ticket, and is filed
# separately. Do not "fix" these by adding no_log -- that hides the diagnostic
# while leaving the leak.
ALLOWLIST: dict[str, str] = {
    "Read the live Redis configuration back, authenticating as the app does": (
        "no_log would not close this task's exposure (the `environment:` prefix "
        "rides the module command line, and SSH: EXEC is not censored). It is "
        "off because the task's own stdout/stderr carry no credential and are "
        "the only Redis-auth diagnostic -- the signal TBD-412 needed."
    ),
    # ⚠ Found by review, not by the first version of this fence, which never
    # opened handlers/ at all.
    "Apply redis live tunables": (
        "Handler, same shape and same reasoning as the read-back above. It "
        "writes the failing CONFIG SET to stderr deliberately, which is the "
        "whole point of the task -- TBD-412 hid for months behind a CONFIG SET "
        "whose result nobody checked, and no_log would swallow exactly that."
    ),
}


def _walk(tasks, out, inherited_no_log=False):
    """Every task dict, descending into block / rescue / always.

    ⚠ THE BLOCK WRAPPER IS APPENDED TOO, stripped of its children, and
    ``no_log: true`` is THREADED DOWN. The first version did neither, and both
    directions were broken:

    * A block carrying ``vars:`` or ``environment:`` holding the credential was
      invisible, because the wrapper's own keys were never examined. Hoisting
      ``environment: REDISCLI_AUTH`` from a task up to its enclosing block --
      an obvious tidy-up -- silently unfenced it.
    * ``no_log: true`` on a block is inherited by every task inside it, which is
      arguably the BETTER way to say "these all handle the credential". The
      fence went RED against that: red against correct code.

    Ansible's precedence: an explicit value on the child wins over the block's.
    """
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        effective = inherited_no_log or task.get("no_log") is True
        children = [k for k in ("block", "rescue", "always") if k in task]
        if children:
            wrapper = {k: v for k, v in task.items() if k not in children}
            if wrapper:
                out.append((wrapper, effective))
            for key in children:
                _walk(task[key], out, effective)
        else:
            out.append((task, effective))
    return out


def _references_secret(node) -> bool:
    """True when any string anywhere in the task names a secret variable."""
    if isinstance(node, str):
        return bool(_SECRET_REF.search(node))
    if isinstance(node, dict):
        return any(_references_secret(v) for v in node.values())
    if isinstance(node, list):
        return any(_references_secret(v) for v in node)
    return False


def _template_renders_secret(task: dict, role_dir: pathlib.Path) -> bool:
    """True for a template task whose SOURCE FILE renders a secret.

    This is the half a task-args-only scan misses entirely: the templates name
    the secret, while the task installing them references only a filename. Both
    of the exposure routes in TBD-414's hazard section live here.

    ⚠ FAILS LOUD, NOT OPEN. If the spec is free-form, ``src`` is not a literal,
    or the resolved file is missing, this RAISES. Returning False would make
    "the check did not run" indistinguishable from "clean" -- which is how a
    ``loop:`` over the two redis templates would silently unfence the drop-in
    that renders ``requirepass``.
    """
    for key, spec in task.items():
        if not key.endswith("template"):
            continue
        if not isinstance(spec, dict):
            raise AssertionError(
                f"template task {task.get('name')!r} uses free-form args; this "
                "fence cannot resolve its src. Use the mapping form."
            )
        src = spec.get("src")
        if not isinstance(src, str) or "{{" in src:
            raise AssertionError(
                f"template task {task.get('name')!r} has a non-literal src "
                f"({src!r}); this fence cannot resolve it. Set no_log "
                "explicitly and allowlist it with a reason."
            )
        candidate = role_dir / "templates" / src
        if not candidate.exists():
            raise AssertionError(
                f"template task {task.get('name')!r} points at {src!r}, which "
                f"does not exist under {role_dir.name}/templates/."
            )
        if _SECRET_REF.search(candidate.read_text()):
            return True
    return False


def _secret_tasks():
    """Every (source, name, task, effective_no_log) that handles a credential."""
    ansible = REPO_ROOT / "infra" / "ansible"
    found = []
    for pattern in TASK_FILE_PATTERNS:
        for path in sorted(ansible.glob(pattern)):
            doc = yaml.safe_load(path.read_text())
            blocks = []
            if pattern.startswith("playbooks") and isinstance(doc, list):
                for play in doc:
                    if isinstance(play, dict):
                        for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                            blocks.append(play.get(key))
            else:
                blocks.append(doc)

            role_dir = path.parent.parent  # where templates/ lives

            for block in blocks:
                for task, effective in _walk(block, []):
                    if _references_secret(task) or _template_renders_secret(
                        task, role_dir
                    ):
                        found.append(
                            (
                                str(path.relative_to(ansible)),
                                task.get("name", "<unnamed>"),
                                task,
                                effective,
                            )
                        )
    return found


def test_every_secret_bearing_task_sets_no_log():
    offenders = [
        f"{source} :: {name}"
        for source, name, task, effective in _secret_tasks()
        if name not in ALLOWLIST and not effective
    ]
    assert offenders == [], (
        "These ansible tasks handle a data-plane credential without `no_log: "
        "true`. A template task prints its RENDERED CONTENT under "
        "`--check --diff`, and a module task echoes its arguments on failure "
        "and under -vvv -- which is how mysql_app_password and redis_password "
        "ended up quotable in a transcript and had to be rotated (TBD-414).\n"
        + "\n".join(offenders)
    )


def test_allowlisted_tasks_still_earn_their_exemption():
    """⚠ ASSERTS THE JUSTIFICATION, NOT THE ENTRY'S EXISTENCE.

    Both allowlisted tasks are exempt for one reason: the credential rides in
    ``environment:``, so it never reaches argv where any ``ps`` on the droplet
    could read it, and keeping ``no_log`` off preserves stderr.

    Keying on the name alone fences existence, not the reason. Measured during
    review: keep the name, move the credential onto argv
    (``redis-cli -a {{ redis_password }}``), and every test stayed GREEN while
    the password became visible to ``ps`` and echoed in the module's ``cmd`` on
    failure -- the exact exposure this ticket exists to close.
    """
    live = {}
    for source, name, task, _ in _secret_tasks():
        if name not in ALLOWLIST:
            continue
        live[name] = source
        stripped = {k: v for k, v in task.items() if k != "environment"}
        assert not _references_secret(stripped), (
            f"{source} :: {name} is allowlisted on the grounds that its "
            "credential rides in `environment:` and therefore never reaches "
            "argv. It is now referenced elsewhere in the task, so that "
            "justification no longer holds. Set no_log, or change the reason."
        )

    # Strict equality, per the repo's ceiling rule: an allowlist that can rot
    # upward is one that dies without telling you.
    assert set(live) == set(ALLOWLIST), (
        "The no_log allowlist and the roles disagree. Stale entries: "
        f"{sorted(set(ALLOWLIST) - set(live))}"
    )


def test_the_scan_actually_reaches_the_credential_tasks():
    """Guards the guard, with STRICT equality rather than a floor.

    A floor (``>= 5``) cannot detect its own death in the direction that
    matters: a secret-bearing task the detector fails to SEE moves the count by
    zero. Pinning the exact set makes an undetected addition, or a detector
    that silently stops resolving templates, visible here.
    """
    names = {name for _, name, _, _ in _secret_tasks()}
    assert names == {
        # cleartext plugin_auth_string
        "Create application MySQL user (host=%)",
        "Create application MySQL user (host=localhost)",
        "Create backup user (read-everything for mysqldump --single-transaction)",
        # templates whose RENDERED CONTENT carries a credential
        "Drop /root/.my.cnf so cron mysqldump runs non-interactively",
        "Drop pfv Redis static config override",
        # credential via `environment:` -- both allowlisted, see above
        "Read the live Redis configuration back, authenticating as the app does",
        "Apply redis live tunables",
    }, f"detected set changed: {sorted(names)}"


def test_a_secret_in_a_comment_is_ignored_but_one_in_a_value_is_not():
    """The parse-not-grep property, asserted in BOTH directions.

    A one-sided version (assert the comment case is False) is green for the
    correct implementation AND for ``_SECRET_REF = re.compile(r"$^")`` -- it
    discriminates nothing. The pair does.
    """
    doc = yaml.safe_load(
        "# requirepass {{ redis_password }} in a comment\n"
        "- name: Innocent task\n"
        "  ansible.builtin.command: /bin/true\n"
        "- name: Guilty task\n"
        "  ansible.builtin.shell: 'redis-cli -a {{ redis_password }} PING'\n"
    )
    innocent, guilty = doc
    assert _references_secret(innocent) is False
    assert _references_secret(guilty) is True
