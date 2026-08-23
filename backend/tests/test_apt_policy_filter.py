"""Table test for the `apt-cache policy` parser (TBD-419).

THIS IS THE ONE FENCE IN TBD-419 WITH TEETH, and the reason the parse was moved
out of a Jinja expression and into a Python filter plugin at all.

A repo-side fence that reads the role's YAML can only assert that the fence task
*calls* these filters. It cannot tell a correct line-anchored parse from the
wrong `'8.4' in policy.stdout` -- and the wrong one is catastrophic in a way
that looks fine: `apt-cache policy` prints the whole version table, so on a host
whose `Candidate:` is `9.0.1` the string `8.4.11` is STILL in stdout. A
substring test therefore waves through precisely the repo drift the fence exists
to catch. Only fixture text can kill that, which is what `POLICY_DRIFTED_TO_9`
below does.

⚠ The fixtures are real `apt-cache policy` output shapes, not invented ones.
`POLICY_UBUNTU_REDIS` was captured from a live `ubuntu:24.04` container in the
session that wrote this file; the MySQL ones mirror the format exactly (name on
line 1, fields indented on lines 2 and 3, version table below).
"""

import importlib.util
import os
import pathlib

import pytest


def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    """⚠ ANCHORED ON A FILE THAT ALREADY EXISTED, NOT ON THE ONE UNDER TEST.

    Anchoring on `filter_plugins/apt_policy.py` would make "the plugin was
    deleted" indistinguishable from "the infra tree is not mounted": the module
    would SKIP instead of failing, and the whole file would go quietly green
    against a tree with no parser in it at all. Measured while writing this --
    the first cut of this module skipped 23 tests when it should have been red.
    """
    for candidate in [start, *start.parents]:
        if (candidate / "infra" / "ansible" / "playbooks" / "site.yml").exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        "infra/ansible/playbooks/site.yml not found from a CI checkout; "
        "this module must not be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason=(
        "the infra tree is not mounted into the backend container; run "
        "`docker compose up -d --force-recreate backend` to pick up the "
        "./infra:/app/infra:ro mount. Always runs in CI."
    ),
)


def _load():
    path = REPO_ROOT / "infra" / "ansible" / "filter_plugins" / "apt_policy.py"
    spec = importlib.util.spec_from_file_location("tbd_apt_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- fixtures ---------------------------------------------------------------

# Production shape: Oracle 8.4.11 installed, same candidate, and Ubuntu's 8.0
# still visible in the table below.
POLICY_PROD_84 = """mysql-community-server:
  Installed: 8.4.11-1ubuntu24.04
  Candidate: 8.4.11-1ubuntu24.04
  Version table:
 *** 8.4.11-1ubuntu24.04 500
        500 http://repo.mysql.com/apt/ubuntu noble/mysql-8.4-lts amd64 Packages
        100 /var/lib/dpkg/status
     8.0.46-0ubuntu0.24.04.3 500
        500 http://archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
"""

# ⚠ THE FIXTURE THAT KILLS THE SUBSTRING BUG. The repo has been re-pointed at
# the Innovation track, so Candidate is 9.0.1 -- but 8.4.11 is still installed
# and therefore still printed in the version table. `'8.4' in stdout` is True
# here. The parse must return 9.0.1.
POLICY_DRIFTED_TO_9 = """mysql-community-server:
  Installed: 8.4.11-1ubuntu24.04
  Candidate: 9.0.1-1ubuntu24.04
  Version table:
     9.0.1-1ubuntu24.04 500
        500 http://repo.mysql.com/apt/ubuntu noble/mysql-innovation amd64 Packages
 *** 8.4.11-1ubuntu24.04 100
        100 /var/lib/dpkg/status
"""

# Scratch droplet: Ubuntu's own 8.0, nothing from Oracle.
POLICY_SCRATCH_80 = """mysql-server:
  Installed: 8.0.46-0ubuntu0.24.04.3
  Candidate: 8.0.46-0ubuntu0.24.04.3
  Version table:
 *** 8.0.46-0ubuntu0.24.04.3 500
        500 http://archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
"""

# An expired repo signing key. ⚠ `apt-get update` exits 0 on this, so its exit
# status is not evidence -- the parse has to surface it.
POLICY_NO_CANDIDATE = """mysql-community-server:
  Installed: 8.4.11-1ubuntu24.04
  Candidate: (none)
  Version table:
 *** 8.4.11-1ubuntu24.04 100
        100 /var/lib/dpkg/status
"""

# Not installed yet (fresh bootstrap).
POLICY_NOT_INSTALLED = """mysql-server:
  Installed: (none)
  Candidate: 8.0.46-0ubuntu0.24.04.3
  Version table:
     8.0.46-0ubuntu0.24.04.3 500
"""

# Captured live from ubuntu:24.04. Carries an EPOCH, which the track parse has
# to strip -- `5:7.0.15` is track 7.0, not 5.0.
POLICY_UBUNTU_REDIS = """redis-server:
  Installed: (none)
  Candidate: 5:7.0.15-1ubuntu0.24.04.4
  Version table:
     5:7.0.15-1ubuntu0.24.04.4 500
        500 http://ports.ubuntu.com/ubuntu-ports noble-updates/universe arm64 Packages
     5:7.0.15-1build2 500
        500 http://ports.ubuntu.com/ubuntu-ports noble/universe arm64 Packages
"""

# `apt-cache policy` on a package apt has never heard of. Measured: exit 0,
# EMPTY stdout. Nothing may be inferred from it except "unreadable".
POLICY_UNKNOWN_PACKAGE = ""

# A table with versions in it but no Candidate line at all. Kills a parse that
# grabs "the first version-looking token".
POLICY_NO_FIELDS = """mysql-community-server:
  Version table:
     9.0.1-1ubuntu24.04 500
"""


# --- the table --------------------------------------------------------------


@pytest.mark.parametrize(
    "name,stdout,installed,candidate",
    [
        ("prod 8.4", POLICY_PROD_84, "8.4.11-1ubuntu24.04", "8.4.11-1ubuntu24.04"),
        ("drifted to 9", POLICY_DRIFTED_TO_9, "8.4.11-1ubuntu24.04", "9.0.1-1ubuntu24.04"),
        ("scratch 8.0", POLICY_SCRATCH_80, "8.0.46-0ubuntu0.24.04.3", "8.0.46-0ubuntu0.24.04.3"),
        ("no candidate", POLICY_NO_CANDIDATE, "8.4.11-1ubuntu24.04", "(none)"),
        ("not installed", POLICY_NOT_INSTALLED, "(none)", "8.0.46-0ubuntu0.24.04.3"),
        ("epoch", POLICY_UBUNTU_REDIS, "(none)", "5:7.0.15-1ubuntu0.24.04.4"),
        ("unknown package", POLICY_UNKNOWN_PACKAGE, "", ""),
        ("no fields", POLICY_NO_FIELDS, "", ""),
    ],
)
def test_fields_are_read_from_their_own_line(name, stdout, installed, candidate):
    """The fields come from the `Installed:`/`Candidate:` LINES, never from the
    version table underneath them."""
    mod = _load()
    assert mod.apt_policy_installed(stdout) == installed, name
    assert mod.apt_policy_candidate(stdout) == candidate, name


def test_a_substring_test_would_pass_on_the_drifted_fixture():
    """Proves POLICY_DRIFTED_TO_9 actually discriminates.

    Without this, `test_the_candidate_track_sees_the_drift` could be passing for
    the wrong reason (a fixture on which right and wrong implementations agree
    proves nothing -- this repo has shipped that shape before).
    """
    assert "8.4" in POLICY_DRIFTED_TO_9, "the fixture no longer traps the substring bug"


def test_the_candidate_track_sees_the_drift():
    mod = _load()
    installed = mod.apt_version_track(mod.apt_policy_installed(POLICY_DRIFTED_TO_9))
    candidate = mod.apt_version_track(mod.apt_policy_candidate(POLICY_DRIFTED_TO_9))
    assert installed == "8.4"
    assert candidate == "9.0"
    assert candidate != installed, "the fence would not fire on a 9.x-drifted repo"


@pytest.mark.parametrize(
    "version,track",
    [
        ("8.4.11-1ubuntu24.04", "8.4"),
        ("8.0.46-0ubuntu0.24.04.3", "8.0"),
        ("9.0.1-1ubuntu24.04", "9.0"),
        # Epoch stripped: this is 7.0, emphatically not 5.0.
        ("5:7.0.15-1ubuntu0.24.04.4", "7.0"),
        ("  8.4.12-1ubuntu24.04  ", "8.4"),
        # Everything that is not a version must come back as "", so the fence's
        # `| length > 0` clause fails closed rather than comparing '' to ''.
        ("(none)", ""),
        ("", ""),
        (None, ""),
        ("garbage", ""),
    ],
)
def test_track_is_major_minor_with_epoch_and_revision_stripped(version, track):
    assert _load().apt_version_track(version) == track


@pytest.mark.parametrize(
    "name,stdout",
    [
        ("no candidate", POLICY_NO_CANDIDATE),
        ("unknown package", POLICY_UNKNOWN_PACKAGE),
        ("no fields", POLICY_NO_FIELDS),
    ],
)
def test_an_unreadable_candidate_yields_an_empty_track(name, stdout):
    """`apt-get update` exits 0 against a repo whose signature failed, so the
    exit status proves nothing and `Candidate: (none)` is a real state. It must
    produce an empty track, which fails the fence's length check, rather than
    something that happens to compare equal to the installed track."""
    mod = _load()
    assert _load().apt_version_track(mod.apt_policy_candidate(stdout)) == "", name


def test_the_filter_plugin_exposes_all_three_filters_to_ansible():
    """The role calls these by name. A rename here is a runtime template error
    on the production data droplet, at the top of a maintenance window."""
    filters = _load().FilterModule().filters()
    assert set(filters) == {
        "apt_policy_installed",
        "apt_policy_candidate",
        "apt_version_track",
    }
