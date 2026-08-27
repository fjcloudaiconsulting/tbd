"""Fence: every ansible task that handles a data-plane credential sets no_log (TBD-414).

⚠⚠ EVERY ASSERTION HERE PARSES THE YAML. None of them greps. The roles carry
long comment blocks that quote the very variable names being fenced -- e.g. the
redis role explains *why* one task deliberately omits ``no_log`` and names
``redis_password`` while doing so. A whole-file grep is satisfied by the comment
documenting its own exception, which is the trap this repo has hit repeatedly.
``yaml.safe_load`` drops comments, so parsing is the only form that means
anything. Same rule as ``test_dataplane_apt_pins.py``.

## The hazard

On 2026-08-19 the plaintext ``mysql_app_password`` and ``redis_password`` were
pasted into an agent transcript while debugging, and had to be rotated on
principle. They became quotable because ``--check --diff`` prints a template's
RENDERED CONTENT, and two templates render credentials:
``root.my.cnf.j2`` (``mysql_backup_password``) and ``00-static.conf.j2``
(``requirepass {{ redis_password }}``). The three ``mysql_user`` tasks are the
same class by a different route: they pass the password to the module as
cleartext ``plugin_auth_string``, which Ansible echoes in task output on
failure and under ``-vvv``.

## What this can and cannot see

It asserts that a task WHICH REFERENCES a secret variable carries ``no_log``.
It cannot see a credential that reaches the box by some path that never names
one of these variables -- a hardcoded literal, or a value assembled from parts.
Do not read a green run as "no credential can be printed"; read it as "no task
naming a known secret variable prints it".
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

_SECRET_REF = re.compile(
    r"\{\{-?\s*(?:" + "|".join(sorted(SECRET_VARS)) + r")\b"
)

ROLES = ("mysql", "redis", "backups", "common")

# ⚠ Tasks that reference a secret and deliberately do NOT set no_log, each with
# the reason recorded in the role itself. Asserted with STRICT equality below:
# a stale entry fails, so this cannot rot upward into a silent exemption.
ALLOWLIST: dict[str, str] = {
    "Read the live Redis configuration back, authenticating as the app does": (
        "Passes the credential through `environment:` rather than argv, so it "
        "is not visible to `ps` on the droplet. Keeping no_log OFF preserves "
        "stderr, which is the only place the real cause of a Redis auth "
        "failure appears -- the exact debugging signal TBD-412 needed."
    ),
}


def _walk(tasks, out):
    """Yield every task dict, descending into block / rescue / always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        nested = False
        for key in ("block", "rescue", "always"):
            if key in task:
                _walk(task[key], out)
                nested = True
        if not nested:
            out.append(task)
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

    This is the half a task-only scan misses: `root.my.cnf.j2` and
    `00-static.conf.j2` name the secret, while the task that installs them
    references only the filename.
    """
    for key, spec in task.items():
        if not key.endswith("template") or not isinstance(spec, dict):
            continue
        src = spec.get("src")
        if not isinstance(src, str):
            continue
        candidate = role_dir / "templates" / src
        if candidate.exists() and _SECRET_REF.search(candidate.read_text()):
            return True
    return False


def _secret_tasks():
    """Every (role, task-name, task) that handles a credential."""
    found = []
    for role in ROLES:
        role_dir = REPO_ROOT / "infra" / "ansible" / "roles" / role
        tasks_file = role_dir / "tasks" / "main.yml"
        if not tasks_file.exists():
            continue
        parsed = yaml.safe_load(tasks_file.read_text())
        for task in _walk(parsed, []):
            if _references_secret(task) or _template_renders_secret(task, role_dir):
                found.append((role, task.get("name", "<unnamed>"), task))
    return found


def test_every_secret_bearing_task_sets_no_log():
    offenders = [
        f"{role}: {name}"
        for role, name, task in _secret_tasks()
        if name not in ALLOWLIST and task.get("no_log") is not True
    ]
    assert offenders == [], (
        "These ansible tasks handle a data-plane credential without `no_log: "
        "true`. A template task prints its RENDERED CONTENT under "
        "`--check --diff`, and a module task echoes its arguments on failure "
        "and under -vvv -- which is how mysql_app_password and redis_password "
        "ended up quotable in a transcript and had to be rotated (TBD-414).\n"
        + "\n".join(offenders)
    )


def test_allowlist_has_no_stale_entries():
    # Strict equality, per the repo's ceiling rule: an allowlist that can rot
    # upward is an allowlist that dies without telling you.
    live = {name for _, name, _ in _secret_tasks() if name in ALLOWLIST}
    assert live == set(ALLOWLIST), (
        "The no_log allowlist and the roles disagree. Stale entries: "
        f"{sorted(set(ALLOWLIST) - live)}"
    )


def test_the_scan_actually_reaches_the_credential_tasks():
    """Guards the guard.

    If the walker stopped descending, or the roles moved, both assertions above
    would pass vacuously while the fence reported green. Pin the specific tasks
    that carry the two exposure routes -- a cleartext module argument and a
    template that renders a credential -- so a rename fails here loudly rather
    than shrinking coverage to nothing.
    """
    names = {name for _, name, _ in _secret_tasks()}
    assert len(names) >= 5, f"scan found only {len(names)} secret-bearing tasks"
    for required in (
        # cleartext plugin_auth_string
        "Create application MySQL user (host=%)",
        "Create application MySQL user (host=localhost)",
        "Create backup user (read-everything for mysqldump --single-transaction)",
        # templates whose rendered content carries a credential
        "Drop /root/.my.cnf so cron mysqldump runs non-interactively",
        "Drop pfv Redis static config override",
    ):
        assert required in names, f"{required!r} no longer detected as secret-bearing"


def test_a_secret_named_only_in_a_comment_is_not_detected():
    """The parse-not-grep property, asserted rather than claimed.

    The roles' own comments quote these variable names while explaining them.
    A grep-based version of this fence would treat those comments as tasks.
    """
    doc = yaml.safe_load(
        "# requirepass {{ redis_password }} in a comment\n"
        "- name: Innocent task\n"
        "  ansible.builtin.command: /bin/true\n"
    )
    assert _references_secret(doc[0]) is False
    assert doc[0]["name"] == "Innocent task"
