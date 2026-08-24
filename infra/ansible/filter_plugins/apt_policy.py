"""Parse `apt-cache policy <pkg>` output into the two fields a fence can reason
about, plus the `major.minor` track of a Debian version string.

TBD-419. WHY THIS IS A PYTHON FILTER AND NOT A JINJA EXPRESSION IN THE ROLE.

A repo-side fence can only assert on what it can see. If the parse lives in a
Jinja expression, the fence sees a STRING, and a string comparison cannot tell
a correct `regex_search('^\\s*Candidate:\\s*(\\S+)', multiline=True)` from the
wrong `'8.4' in policy.stdout`. The second one looks fine and is catastrophic:
`apt-cache policy` prints the entire version table, so on a host whose
`Candidate:` is `9.0.1` the string `8.4.11` is STILL somewhere in stdout, and a
substring test waves through precisely the repo drift the fence exists to catch.

Only a table test against real fixture text kills that, and a table test needs a
real function. `backend/tests/test_apt_policy_filter.py` is that test; it is the
one fence in this ticket with teeth, because the YAML-shape fences around it can
only check that the role CALLS these filters.

⚠ The `^` anchors are multiline on purpose. `apt-cache policy` puts the package
name on line 1 and the fields on lines 2 and 3. Without `re.M` both searches
return nothing, every downstream assertion compares '' to '' and the whole
fence goes vacuously GREEN on a drifted repo.
"""

from __future__ import annotations

import re

# Anchored to the start of a line so a version sitting in the version table
# below can never be mistaken for the field's value.
_FIELD = r"^[ \t]*{}:[ \t]*(\S+)[ \t]*$"

_INSTALLED = re.compile(_FIELD.format("Installed"), re.M)
_CANDIDATE = re.compile(_FIELD.format("Candidate"), re.M)

# Debian versions may carry an epoch (`5:7.0.15-1build2`) and always carry a
# revision (`-1ubuntu24.04`). The track is the first two dot-separated numeric
# components of the upstream version, with both of those stripped.
_TRACK = re.compile(r"^(?:\d+:)?(\d+)\.(\d+)")


def _field(stdout: str | None, pattern: re.Pattern[str]) -> str:
    """Return the field's raw token, or '' when apt printed no such line.

    ⚠ Returns the literal '(none)' rather than normalising it away. "no
    candidate at all" (a repo whose signature failed -- and `apt-get update`
    exits 0 on that) and "not installed" (a fresh host, legitimately) are
    different situations that need different messages, so the caller
    distinguishes them. Collapsing both to '' here is how one of them ends up
    silently treated as the other.
    """
    if not stdout:
        return ""
    match = pattern.search(stdout)
    return match.group(1) if match else ""


def apt_policy_installed(stdout: str | None) -> str:
    """The `Installed:` field of `apt-cache policy` output, or ''."""
    return _field(stdout, _INSTALLED)


def apt_policy_candidate(stdout: str | None) -> str:
    """The `Candidate:` field of `apt-cache policy` output, or ''."""
    return _field(stdout, _CANDIDATE)


def apt_version_track(version: str | None) -> str:
    """`major.minor` of a Debian version, or '' if there is not one.

    '8.4.11-1ubuntu24.04' -> '8.4'      '(none)' -> ''
    '5:7.0.15-1build2'    -> '7.0'      ''       -> ''
    """
    if not version:
        return ""
    match = _TRACK.match(version.strip())
    return f"{match.group(1)}.{match.group(2)}" if match else ""


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "apt_policy_installed": apt_policy_installed,
            "apt_policy_candidate": apt_policy_candidate,
            "apt_version_track": apt_version_track,
        }
