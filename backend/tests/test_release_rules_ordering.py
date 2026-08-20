"""Fence the ORDER of `.releaserc.json`'s `releaseRules` (TBD-424).

## The defect this exists to stop

`@semantic-release/commit-analyzer` does NOT stop at the first matching rule.
`lib/analyze-commit.js` filters EVERY matching rule and folds them in ARRAY
ORDER through `lib/compare-release-types.js`:

    (current, candidate) =>
        !current || RELEASE_TYPES.indexOf(candidate) < RELEASE_TYPES.indexOf(current)

Two consequences, both counter-intuitive, both measured against the real
package (13.0.1) rather than reasoned about:

1. ``!current`` is TRUE when ``current`` is ``False``. So a suppression that is
   already set gets OVERWRITTEN by any later matching rule.
2. ``RELEASE_TYPES.indexOf(false)`` is ``-1``, lower than every real type. So a
   suppression WINS when it comes LATER.

Therefore a `release: false` rule only suppresses if it sits AFTER every rule
carrying a real release type that could also match the same commit.

Before this fence, the `scope` suppressions sat BEFORE the type rules, so five
consecutive `fix(infra)` commits were each rated `patch` and cut v0.258.1 --
verified in the semantic-release log of run 32346696930. `{"type":"perf",
"release":false}` was the ONLY suppression that worked, purely because it
happened to be last in the file.

## Why an ordering invariant rather than a truth table

Reproducing the analyzer's decision in Python would be a SECOND implementation
of a third-party algorithm, free to drift from the real one while staying
green. The invariant below is the property the real algorithm requires, stated
once. The behavioural truth table was produced by driving the actual npm
package, and lives in the TBD-424 PR body and Jira comment.

⚠ `.releaserc.json` is JSON and cannot carry comments, which is exactly why the
rationale lives here: a maintainer who reorders those rules is sent to this
file by the failure message.
"""

import json
import os
import pathlib

import pytest

def _find_repo_root(start: pathlib.Path) -> pathlib.Path | None:
    """Walk upward until a directory holding `.releaserc.json` is found.

    `parents[2]` works from a host checkout but resolves to `/` inside the
    backend container, where this file lives at `/app/tests/`. Only
    `backend/` subtrees are bind-mounted there, so `.releaserc.json` is
    genuinely absent and there is nothing to assert against.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".releaserc.json").exists() and (candidate / ".github").is_dir():
            return candidate
    return None


REPO_ROOT = _find_repo_root(pathlib.Path(__file__).resolve())

# ⚠ Skipping is correct in the dev container and FATAL in CI. A fence that
# quietly skips on the runner is worse than no fence: it reports green while
# asserting nothing, which is this repo's most-repeated defect class. So the
# skip is allowed only where the file is legitimately unmounted, and CI
# (GITHUB_ACTIONS=true, where pytest runs on a plain full checkout) must find
# it or fail loudly.
if REPO_ROOT is None and os.environ.get("GITHUB_ACTIONS") == "true":  # pragma: no cover
    raise RuntimeError(
        ".releaserc.json not found from a CI checkout. These fences must not "
        "be allowed to skip on the runner."
    )

pytestmark = pytest.mark.skipif(
    REPO_ROOT is None,
    reason=".releaserc.json is not mounted into the backend container; runs in CI",
)

RELEASERC = (REPO_ROOT / ".releaserc.json") if REPO_ROOT else pathlib.Path()


def _release_rules() -> list[dict]:
    doc = json.loads(RELEASERC.read_text())
    for plugin in doc["plugins"]:
        if isinstance(plugin, list) and plugin[0] == "@semantic-release/commit-analyzer":
            return plugin[1]["releaseRules"]
    raise AssertionError("commit-analyzer plugin block not found in .releaserc.json")


def test_the_config_is_shaped_as_this_module_assumes():
    """Positive baseline. Without it, a moved key or a renamed plugin yields an
    empty rule list and every ordering assertion below passes vacuously -- the
    unreachable-predicate class this repo has shipped before."""
    rules = _release_rules()
    assert len(rules) >= 15, f"parsed only {len(rules)} rule(s); expected >= 15"
    assert any(r.get("release") for r in rules), "no rule grants a release at all"
    assert any(not r.get("release") for r in rules), "no suppression rule at all"
    assert any(r.get("scope") for r in rules), "no scope-based rule at all"


def test_no_suppression_precedes_a_rule_that_grants_a_release():
    """THE fence. A `release: false` rule placed before a rule with a real
    release type is DEAD: any later match overwrites it, because `!False` is
    true. This went RED against the pre-TBD-424 order."""
    rules = _release_rules()
    offenders = []
    for index, rule in enumerate(rules):
        if rule.get("release"):
            continue
        later_grants = [r for r in rules[index + 1:] if r.get("release")]
        if later_grants:
            offenders.append((index, rule, later_grants[0]))

    assert not offenders, (
        "These `release: false` rules sit BEFORE a rule that grants a release, "
        "so commit-analyzer overwrites them and they suppress NOTHING:\n"
        + "\n".join(
            f"  index {i}: {rule!r} is overwritten by a later {grant!r}"
            for i, rule, grant in offenders
        )
        + "\n\nMove every suppression AFTER every rule carrying a real release "
        "type. See this module's docstring for the mechanism."
    )


def test_a_breaking_change_is_never_suppressed():
    """A scope suppression must never swallow a breaking change. The `breaking`
    rule survives from any position (`!False` is true, so `major` reclaims it),
    but keeping it first also triggers analyze-commit's early stop."""
    rules = _release_rules()
    breaking = [r for r in rules if r.get("breaking")]
    assert len(breaking) == 1, f"expected exactly one breaking rule, got {breaking}"
    assert breaking[0].get("release") == "major"
    assert rules[0] == breaking[0], (
        "the breaking rule must stay FIRST: analyze-commit stops iterating as "
        "soon as `major` is set, which is the cheapest protection available."
    )


@pytest.mark.parametrize("scope", ["infra", "ci", "deps-dev", "test", "tests", "dev"])
def test_every_intended_scope_suppression_is_still_present(scope):
    """Pins the suppressed set. Reordering must not silently drop one, and a
    future edit that removes a scope should be a deliberate two-place change."""
    rules = _release_rules()
    assert any(r.get("scope") == scope and not r.get("release") for r in rules), (
        f"scope {scope!r} is no longer suppressed"
    )


def test_deps_is_deliberately_NOT_suppressed():
    """A production dependency bump MUST cut a release, because deploy is gated
    on one.

    `release.yml` runs its deploy job under
    `if: needs.release.outputs.new_release_published == 'true'`. So a commit
    that cuts no release also **never deploys**. `deps` bumps change the
    shipped image, so suppressing them would leave a dependency fix -- a
    security patch, typically -- tested and merged but sitting on `main`
    undeployed until some unrelated commit happened to cut a release and drag
    it out. Silent, and unbounded in duration.

    `deps-dev` stays suppressed: dev/build tooling is not in the runtime
    artifact, and if it changes emitted output the next real release carries
    it. `infra` stays suppressed because Terraform/Ansible are applied outside
    App Platform entirely.

    This is a NEGATIVE fence on purpose. Re-adding `{"scope": "deps"}` is a
    one-line edit that looks like tidying up an inconsistency, and it would
    silently reopen the hole.
    """
    rules = _release_rules()
    offenders = [r for r in rules if r.get("scope") == "deps" and not r.get("release")]
    assert not offenders, (
        "`deps` must NOT be release-suppressed: deploy is gated on "
        "`new_release_published`, so suppressing it means production never "
        "receives the dependency bump. See this test's docstring."
    )
