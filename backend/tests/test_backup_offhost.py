"""Fences for the off-host MySQL backup (TBD-400).

The hazard: the nightly dump was written to /var/backups/mysql on the droplet it
protects, with droplet snapshots off, so a disk or droplet loss took the data and
its only backup together. Fixing that adds three things that can each fail
silently -- a verification, an upload, and an alarm -- so each is fenced by being
EXECUTED, not by being read.

⚠⚠ THE VERIFICATION AND THE PROBE ARE REAL FILES, NOT JINJA, SPECIFICALLY SO
THESE TESTS CAN RUN THEM. Logic embedded in a `.j2` can only ever be
grep-fenced, and in this repo a grep is routinely satisfied by a comment -- both
scripts carry long comments naming the very strings a grep would look for.

⚠ A behavioural test found a real defect here that a structural one could not:
`check-backup-freshness.sh` originally ran its evaluator as `python3 - <<'PY'`,
which makes the HEREDOC stdin, so the piped S3 listing never reached the program
and every input, healthy or not, was judged "listing has no Contents key". A
fence asserting the script mentions "Contents" would have passed it.
"""

import json
import os
import pathlib
import re
import subprocess

import pytest


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
        "`docker compose up -d --force-recreate backend`. Always runs in CI."
    ),
)

BACKUPS_ROLE = "infra/ansible/roles/backups"


def _p(rel: str) -> pathlib.Path:
    """Resolve a repo-relative path, honouring the container's mount layout.

    ⚠ `/app/scripts` inside the backend container is the BACKEND's own scripts
    package, not the repo-root `scripts/`. The repo-root one is mounted at
    `/app/repo-scripts` (docker-compose.yml), the same convention
    test_ci_gate_accept_rule.py and test_await_test_run_gate.py already use.
    Resolving `scripts/...` against REPO_ROOT would silently point at the wrong
    directory and fail with "No such file".
    """
    if rel.startswith("scripts/"):
        mounted = pathlib.Path("/app/repo-scripts") / rel[len("scripts/"):]
        if mounted.exists():
            return mounted
    return REPO_ROOT / rel


def _script_lines(rel: str) -> list[str]:
    """Executable lines only. Comment-stripping is the whole point: both files
    document the strings these fences look for."""
    return [ln for ln in _p(rel).read_text().splitlines() if not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# F1. The verifier actually rejects each bad artifact class.
# ---------------------------------------------------------------------------
VERIFY = f"{BACKUPS_ROLE}/files/mysql-backup-verify.sh"

# ⚠⚠ THE BACKTICK FORM IS WHAT PRODUCTION ACTUALLY EMITS, and getting this
# fixture wrong shipped a defect that this very suite reported green.
#
# MySQL's SHOW CREATE USER quotes identifiers with BACKTICKS:
#
#   CREATE USER `pfv_app`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '...'
#
# The original fixture invented the single-quoted form, so the verifier's
# single-quote grep matched the fixture, passed every test, and then refused
# EVERY REAL BACKUP on the first production run. A fabricated fixture that does
# not match the shape the real producer emits is not a test of that producer.
#
# Both forms are pinned below, and the backtick one is listed first because it
# is the real one.
GRANTS_BACKTICK = (
    b"-- grants\n"
    b"CREATE USER `pfv_app`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '$A$005$x';\n"
    b"GRANT ALL ON pfv2.* TO `pfv_app`@`%`;\n"
)
GRANTS_SINGLE_QUOTED = (
    b"-- grants\n"
    b"CREATE USER 'pfv_app'@'%' IDENTIFIED WITH 'caching_sha2_password' AS '$A$005$x';\n"
    b"GRANT ALL ON pfv2.* TO 'pfv_app'@'%';\n"
)
GOOD_GRANTS = GRANTS_BACKTICK
GRANTS_NO_APP = b"-- grants\nGRANT USAGE ON *.* TO `someone`@`%`;\n"


def _gz(tmp_path, name: str, payload: bytes) -> pathlib.Path:
    import gzip
    path = tmp_path / name
    with gzip.open(path, "wb") as fh:
        fh.write(payload)
    return path


def _dump_bytes(tables: int, complete: bool = True) -> bytes:
    out = [b"-- MySQL dump 10.13\n"]
    for i in range(tables):
        out.append(f"CREATE TABLE `t{i}` (id int);\n".encode())
    if complete:
        out.append(b"-- Dump completed on 2026-08-28  2:00:01\n")
    return b"".join(out)


def _verify(tmp_path, dump: pathlib.Path, grants: pathlib.Path, expected: str):
    return subprocess.run(
        ["bash", str(_p(VERIFY)), str(dump), str(grants), str(expected)],
        capture_output=True, text=True,
    )


def test_verifier_accepts_a_healthy_pair(tmp_path):
    """The inverse defect: a gate that rejects everything is not a gate.

    Also pins the pipefail trap -- `zcat BIG | grep -q PAT` FAILS on a good file
    because grep exits early and zcat takes SIGPIPE, so a 'simplification' to
    grep -q would turn this green case red.
    """
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(50)),
                _gz(tmp_path, "g.gz", GOOD_GRANTS), "50")
    assert r.returncode == 0, f"healthy pair rejected:\n{r.stdout}\n{r.stderr}"


def test_verifier_rejects_a_truncated_dump_that_gzip_accepts(tmp_path):
    """⚠ THE DANGEROUS CASE. A producer that exits non-zero mid-stream lets gzip
    see EOF and write its trailer, so `gzip -t` PASSES on a truncated dump with a
    plausible size. The completion marker is the only in-band tell."""
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(50, complete=False)),
                _gz(tmp_path, "g.gz", GOOD_GRANTS), "50")
    assert r.returncode == 1
    assert "Dump completed" in r.stderr


def test_verifier_rejects_a_partial_schema(tmp_path):
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(49)),
                _gz(tmp_path, "g.gz", GOOD_GRANTS), "50")
    assert r.returncode == 1
    assert "49" in r.stderr and "50" in r.stderr


@pytest.mark.parametrize(
    "grants",
    [GRANTS_BACKTICK, GRANTS_SINGLE_QUOTED],
    ids=["backtick-as-production-emits", "single-quoted"],
)
def test_verifier_accepts_the_grants_quoting_mysql_actually_produces(tmp_path, grants):
    """⚠ REGRESSION FENCE for a defect that shipped.

    `SHOW CREATE USER` emits backticks. The verifier grepped for single quotes,
    so it refused every real backup while this suite stayed green against a
    fixture that invented the single-quoted form. Both are pinned now, and the
    backtick case is the one that reflects production.
    """
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(50)),
                _gz(tmp_path, "g.gz", grants), "50")
    assert r.returncode == 0, (
        f"the verifier rejected grants that MySQL really produces:\n{r.stderr}"
    )


def test_verifier_rejects_grants_without_the_app_account(tmp_path):
    """A grants file without pfv_app restores tables and zero logins, which is
    the hole this artifact exists to close."""
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(50)),
                _gz(tmp_path, "g.gz", GRANTS_NO_APP), "50")
    assert r.returncode == 1
    assert "pfv_app" in r.stderr


def test_verifier_rejects_a_corrupt_gzip(tmp_path):
    bad = tmp_path / "d.gz"
    bad.write_bytes(b"this is not gzip")
    r = _verify(tmp_path, bad, _gz(tmp_path, "g.gz", GOOD_GRANTS), "50")
    assert r.returncode == 1


@pytest.mark.parametrize(
    "expected", ["fifty", "0", "-1"],
    ids=["non-numeric", "zero", "negative"],
)
def test_verifier_refuses_a_nonsensical_table_count(tmp_path, expected):
    """Exit 2, not 1: a bad argument is 'could not check', which must not be
    confused with 'the backup is bad'."""
    r = _verify(tmp_path,
                _gz(tmp_path, "d.gz", _dump_bytes(50)),
                _gz(tmp_path, "g.gz", GOOD_GRANTS), expected)
    assert r.returncode == 2


SCRIPT = f"{BACKUPS_ROLE}/templates/mysql-backup.sh.j2"


def _invocation(token: str) -> tuple[int, str]:
    """Find the line where `token` is INVOKED, not merely mentioned.

    ⚠ An earlier version of the ordering fences used "first line matching the
    token", which an `echo "will verify with {{ ... }}"` satisfies. That let a
    mutant publish the dump at its final name and verify afterwards -- the exact
    defect the fence's own message describes -- while staying green. Command
    position is the property; a mention is not.
    """
    lines = _script_lines(SCRIPT)
    hits = [
        (i, ln) for i, ln in enumerate(lines)
        if re.match(r"^\s*" + re.escape(token) + r"(\s|$)", ln)
    ]
    assert len(hits) == 1, (
        f"expected exactly one invocation of {token!r} in command position, "
        f"found {len(hits)}: {[h[1].strip() for h in hits]}"
    )
    return hits[0]


def test_the_table_count_is_read_live_and_passed_through():
    """Kills: a literal count, in EITHER place.

    50 is a measurement of today's schema, not an invariant, so a literal turns
    the next migration into a red check against a perfectly good backup. ⚠ It is
    not enough to check the assignment: a mutant kept `EXPECTED_TABLES=$(mysql
    ...)` intact and passed a literal `50` to the verifier instead, which
    satisfied an assignment-only fence.
    """
    body = "\n".join(_script_lines(SCRIPT))
    assert "information_schema.tables" in body, (
        "the backup script no longer reads the table count live."
    )
    assert not re.search(r"EXPECTED_TABLES=\s*['\"]?\d", body), (
        "the expected table count is hardcoded in the backup script."
    )
    _, line = _invocation("{{ mysql_backup_verify_script }}")
    args = line.split()
    assert args[-1] == '"${EXPECTED_TABLES}"', (
        f"the verifier is called with {args[-1]!r} as its table count rather "
        'than "${EXPECTED_TABLES}". A literal there is the same date bomb, one '
        "argument to the right."
    )


# ---------------------------------------------------------------------------
# F2. The freshness probe distinguishes fresh / stale / could-not-run.
# ---------------------------------------------------------------------------
PROBE = "scripts/ci/check-backup-freshness.sh"
PREFIX = "pfv-data-01/2026/08/27"


def _listing(age_hours: float, *, manifest=True, grants=True, dump_size=620000, now=1000000000):
    import datetime
    ts = datetime.datetime.fromtimestamp(
        now - age_hours * 3600, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    objs = [{"Key": f"{PREFIX}/pfv2_x.sql.gz", "Size": dump_size, "LastModified": ts}]
    if grants:
        objs.append({"Key": f"{PREFIX}/grants_x.sql.gz", "Size": 800, "LastModified": ts})
    if manifest:
        objs.append({"Key": f"{PREFIX}/manifest_x.json", "Size": 484, "LastModified": ts})
    return json.dumps({"Contents": objs})


def _probe(payload: str, now=1000000000):
    return subprocess.run(
        ["bash", str(_p(PROBE))],
        input=payload, capture_output=True, text=True,
        env={**os.environ, "NOW_EPOCH": str(now)},
    )


def test_probe_reports_fresh_for_a_complete_recent_night():
    r = _probe(_listing(2))
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    assert "fresh" in r.stdout


def test_probe_reports_stale_after_one_missed_night():
    """26h is one missed run. The threshold must alarm on ONE miss, not two."""
    r = _probe(_listing(26))
    assert r.returncode == 1
    assert "STALE" in r.stdout


def test_probe_reports_stale_when_the_manifest_is_missing():
    """⚠ The manifest is uploaded LAST, so its absence is the only evidence
    distinguishing 'the night completed' from 'the dump uploaded and then the
    grants upload died'. A presence check on the dump alone is fail-open."""
    r = _probe(_listing(2, manifest=False))
    assert r.returncode == 1
    assert "manifest" in r.stdout


def test_probe_reports_stale_when_grants_are_missing():
    r = _probe(_listing(2, grants=False))
    assert r.returncode == 1


def test_probe_reports_stale_for_an_implausibly_small_dump():
    """'Some object exists' reads a bucket of tiny stubs as healthy forever."""
    r = _probe(_listing(2, dump_size=12))
    assert r.returncode == 1


def test_probe_reports_stale_for_an_empty_bucket():
    r = _probe(json.dumps({"Contents": []}))
    assert r.returncode == 1


@pytest.mark.parametrize(
    "payload", ["not json", '{"Name": "b"}', "", '{"IsTruncated": true, "Contents": []}'],
    ids=["malformed", "not-a-listing", "empty-stdin", "truncated"],
)
def test_probe_reports_could_not_run_rather_than_healthy(payload):
    """⚠ Exit 2, never 0. A probe that cannot answer must not be mistaken for a
    probe that answered 'fine'.

    ⚠ A TRUNCATED listing is in here deliberately: answering from half the
    objects could miss the newest page entirely. The CLI auto-paginates today,
    so this only bites if someone adds --max-items -- which is exactly the kind
    of change that would otherwise pass review."""
    r = _probe(payload)
    assert r.returncode == 2, f"rc={r.returncode} out={r.stdout}"


def test_probe_reports_stale_for_a_genuinely_empty_bucket():
    """⚠ `aws s3api list-objects-v2` OMITS Contents for an empty result rather
    than emitting `"Contents": []`, so the real-world empty bucket arrives as
    `{"KeyCount": 0, ...}`. Classifying that as 'could not run' told the
    operator the wrong thing about a bucket that is genuinely, alarmingly
    empty."""
    r = _probe('{"KeyCount": 0, "Name": "tbd-mysql-backups-884686184019"}')
    assert r.returncode == 1, f"rc={r.returncode} out={r.stdout}"
    assert "empty" in r.stdout.lower()


def test_the_probe_workflow_alarms_on_could_not_run_too():
    """Kills: only failing the job. A red scheduled workflow notifies nobody,
    and nothing else in this repo covers this signal."""
    import yaml
    wf = yaml.safe_load(_p(".github/workflows/backup-freshness-probe.yml").read_text())
    steps = wf["jobs"]["probe"]["steps"]
    alarm = [s for s in steps if "notify-backup-stale.sh" in str(s.get("run", ""))]
    assert alarm, "the workflow never invokes the alarm script."
    # ⚠ Normalized EXACT match, not a substring. `verdict == 'stale' &&
    # verdict != 'fresh-x'` contains both "!=" and "fresh" and stayed green,
    # while silencing the could-not-run verdict -- the one this test is named
    # after, and the one that fires while the workspace is still unapplied.
    cond = " ".join(str(alarm[0].get("if", "")).split())
    assert cond == "steps.check.outputs.verdict != 'fresh'", (
        f"the alarm fires on {cond!r}. It must be exactly "
        "\"steps.check.outputs.verdict != 'fresh'\" so that ANY non-fresh "
        "verdict, including could-not-run, raises the alarm."
    )
    # ⚠ PyYAML parses the workflow key `on:` as the BOOLEAN True (YAML 1.1
    # treats on/off/yes/no as booleans), so wf["on"] raises KeyError on a
    # perfectly valid workflow. Accept either key.
    triggers = wf.get("on", wf.get(True))
    assert triggers, "could not read the workflow's triggers."
    assert triggers.get("schedule"), (
        "the probe has no schedule, so it detects no silence -- which is the "
        "only failure mode it exists to catch."
    )


# ---------------------------------------------------------------------------
# F3. The uploader is put-only, and stays that way.
# ---------------------------------------------------------------------------
UPLOADER_POLICY = "infra/terraform/backups/policies/backup-uploader.json"
PROBE_POLICY = "infra/terraform/backups/policies/backup-probe.json"


def _actions(rel: str) -> set[str]:
    doc = json.loads(_p(rel).read_text())
    out: set[str] = set()
    for stmt in doc["Statement"]:
        action = stmt["Action"]
        out.update(action if isinstance(action, list) else [action])
    return out


def test_the_uploader_policy_is_exactly_put_and_encrypt():
    """Exact set, BOTH directions. This is the fence that catches a future
    s3:GetObject widening -- the droplet is the most exposed machine in the
    system, and read access would let a compromise harvest every historical
    password-hash set."""
    assert _actions(UPLOADER_POLICY) == {
        "s3:PutObject", "kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey",
    }


def test_the_probe_policy_is_exactly_list():
    assert _actions(PROBE_POLICY) == {"s3:ListBucket"}


@pytest.mark.parametrize("policy", [UPLOADER_POLICY, PROBE_POLICY],
                         ids=["uploader", "probe"])
def test_the_policies_grant_and_are_scoped(policy):
    """⚠ Pinning Action alone says nothing about WHAT may be done WHERE.

    A mutant flipped Effect to Deny and widened Resource to `arn:aws:s3:::*/*`
    while keeping the action set identical, and stayed green. Effect and
    Resource are as load-bearing as Action.
    """
    doc = json.loads(_p(policy).read_text())
    for stmt in doc["Statement"]:
        assert stmt["Effect"] == "Allow", (
            f"{policy} contains a {stmt['Effect']} statement; these are grant "
            "policies and a Deny here would silently disable the feature."
        )
        resources = stmt["Resource"]
        for resource in resources if isinstance(resources, list) else [resources]:
            assert "${bucket}" in resource or "${kms_key_arn}" in resource, (
                f"{policy} grants on {resource!r}, which is not scoped to this "
                "bucket or key. A wildcard here would let the droplet's "
                "credential act on every bucket in the account."
            )


def test_the_uploader_must_name_the_encryption_key_explicitly():
    """The policy conditions on the SSE headers with StringEquals, and
    StringEquals against an ABSENT header FAILS. Relying on the bucket default
    does not satisfy it, and the resulting 403 reads like a credential problem.
    So the uploader has to send both, explicitly."""
    doc = json.loads(_p(UPLOADER_POLICY).read_text())
    put = [s for s in doc["Statement"] if "s3:PutObject" in str(s["Action"])][0]
    cond = put["Condition"]["StringEquals"]
    assert cond["s3:x-amz-server-side-encryption"] == "aws:kms"
    assert "kms-key-id" in " ".join(cond.keys())

    # ⚠ PARSED, NOT GREPPED. The uploader's comments discuss
    # ServerSideEncryption, SSEKMSKeyId and ChecksumAlgorithm at length, so a
    # substring check over its source is satisfied by prose explaining their
    # absence -- mutants that deleted the real kwargs and left a `# TODO: re-add
    # ServerSideEncryption=...` comment stayed green.
    kwargs = _put_object_kwargs()
    for required in ("ServerSideEncryption", "SSEKMSKeyId"):
        assert required in kwargs, (
            f"put_object does not pass {required}. The IAM policy conditions on "
            "that header with StringEquals, and StringEquals against an ABSENT "
            "header FAILS -- every upload would 403 in a way that reads like a "
            "credential problem."
        )
    assert kwargs.get("ChecksumAlgorithm") == "SHA256", (
        f"put_object passes ChecksumAlgorithm={kwargs.get('ChecksumAlgorithm')!r}. "
        "That checksum IS the transport verification: S3 recomputes it "
        "server-side and rejects a mismatch, which is how the uploaded object is "
        "verified WITHOUT read permission."
    )


# ---------------------------------------------------------------------------
# F4. Ordering inside the backup script.
# ---------------------------------------------------------------------------
def test_nothing_is_published_or_uploaded_before_it_is_verified():
    lines = _script_lines(SCRIPT)
    verify, verify_line = _invocation("{{ mysql_backup_verify_script }}")
    upload, _ = _invocation("{{ mysql_backup_upload_script }}")

    # ⚠ Anchored on the dump's OWN rename, not on `^\s*mv\s`. The loose form
    # went red when any unrelated `mv` appeared earlier in the script -- an
    # inverse defect that punishes a correct change.
    publish = next(
        (i for i, ln in enumerate(lines) if re.match(r'^\s*mv\s+"\$\{DUMP\}\.part"', ln)),
        None,
    )
    assert publish is not None, "the dump is never renamed into place."
    assert verify < publish, (
        "the dump is renamed into place BEFORE it is verified, so a bad dump is "
        "published at the final name."
    )
    assert verify < upload, (
        "the dump is uploaded BEFORE it is verified, which would replicate a "
        "corrupt artifact off-host and mark it verified."
    )

    # ⚠ The verdict must GATE. `... || true` appended to the invocation left
    # every other assertion here green while making the whole feature -- "no
    # artifact is published or uploaded unless it verifies" -- a no-op.
    assert not re.search(r"\|\||&&|;\s*true", verify_line), (
        f"the verifier invocation is not a bare gating command: {verify_line.strip()!r}. "
        "With `|| true` (or similar) its verdict is discarded and a failed "
        "verification no longer stops the publish."
    )
    body = "\n".join(lines)
    assert re.search(r"set\s+-\w*e", body), (
        "the script does not `set -e`, so a failing verifier would not stop it."
    )
    assert "set +e" not in body, (
        "the script disables errexit somewhere, which can un-gate the verifier."
    )


def test_the_manifest_is_uploaded_last():
    """S3 has no rename, so the .part trick does not lift. The manifest's
    presence is the completion marker; uploading it first would mark a night
    complete before its artifacts existed."""
    lines = _script_lines(SCRIPT)
    start, _ = _invocation("{{ mysql_backup_upload_script }}")
    # The invocation is line-continued; collect it to the first line that does
    # not end in a backslash.
    chunk = []
    for ln in lines[start:]:
        chunk.append(ln)
        if not ln.rstrip().endswith("\\"):
            break
    args = " ".join(chunk)
    for artifact in ("${DUMP}", "${GRANTS}"):
        assert args.index(artifact) < args.index("${MANIFEST}"), (
            f"{artifact} is uploaded after the manifest. The manifest is the "
            "completion marker, so it must be last."
        )


def test_the_backup_script_never_reads_an_object_back():
    """The credential cannot read, by construction. A get/copy-back would 403 in
    production and is a sign someone tried to verify the wrong way."""
    body = "\n".join(_script_lines(f"{BACKUPS_ROLE}/templates/mysql-backup.sh.j2"))
    for forbidden in ("get-object", "s3 cp s3://", "download_file", "get_object"):
        assert forbidden not in body, f"the backup script tries to read back: {forbidden}"


# ---------------------------------------------------------------------------
# F5. The TBD-372 trust-anchor fence. This is the one that PREVENTS the event.
# ---------------------------------------------------------------------------
def test_the_trust_document_names_the_workspace_this_configuration_declares():
    """TBD-372: the apex OIDC role's trust policy is managed BY the workspace it
    authorizes, so a rename applied with the pattern unchanged denied
    AssumeRoleWithWebIdentity and the workspace could not apply its own fix.

    Non-management would only have made recovery a one-liner. Comparing the two
    at PR time stops the rename from ever being applied."""
    versions = _p("infra/terraform/backups/versions.tf").read_text()
    declared = re.search(r'workspaces\s*\{[^}]*name\s*=\s*"([^"]+)"', versions, re.DOTALL)
    assert declared, "could not find the workspace name in versions.tf"
    workspace = declared.group(1)

    trust = json.loads(_p("infra/aws/bootstrap/tfc-backups-trust.json").read_text())
    subs = [
        v
        for stmt in trust["Statement"]
        for cond in stmt.get("Condition", {}).values()
        for k, v in cond.items()
        if k.endswith(":sub")
    ]
    assert subs, "the trust document has no sub condition at all."
    # ⚠ ANY, not ALL. The documented safe rename is "widen the pattern to span
    # both names, apply, rename, then narrow" -- and an ALL predicate makes both
    # widened forms fail at PR time, so the only way past CI would be to rename
    # FIRST, which is precisely the TBD-372 lockout this fence exists to
    # prevent. The property is "the declared workspace is authorized by at least
    # one statement", not "no other workspace is".
    assert any(f"workspace:{workspace}:" in sub for sub in subs), (
        f"versions.tf declares workspace {workspace!r} but no committed trust "
        f"statement authorizes it (subs: {subs}). Applying this would deny the "
        "workspace its own role and it could not apply the fix (TBD-372). "
        "Widen the pattern, apply, rename, then narrow."
    )


def test_the_trust_document_does_not_glob_the_workspace_name():
    """apex carries `tbd-apex*` as scar tissue from the rename that caused
    TBD-372. A fresh anchor should not inherit a wildcard on the segment that IS
    the trust boundary."""
    versions = _p("infra/terraform/backups/versions.tf").read_text()
    declared = re.search(r'workspaces\s*\{[^}]*name\s*=\s*"([^"]+)"', versions, re.DOTALL)
    workspace = declared.group(1)
    trust = _p("infra/aws/bootstrap/tfc-backups-trust.json").read_text()
    # ⚠ Scoped to a glob that would MATCH the declared name. A widened pattern
    # naming a DIFFERENT workspace is the legitimate mid-rename state; banning
    # every glob outright would forbid it.
    for sub in re.findall(r'"app\.terraform\.io:sub":\s*"([^"]+)"', trust):
        seg = re.search(r"workspace:([^:]+):", sub)
        if not seg:
            continue
        pattern = seg.group(1)
        if "*" in pattern and pattern.split("*")[0] and workspace.startswith(pattern.split("*")[0]):
            raise AssertionError(
                f"the workspace segment {pattern!r} is a glob matching the "
                f"declared workspace {workspace!r}. apex carries such a wildcard "
                "only as scar tissue from the rename that caused TBD-372; a "
                "fresh anchor must name its workspace exactly."
            )


# ---------------------------------------------------------------------------
# F7. The uploader is DRIVEN, not read. Its own docstring justifies being a real
# file on the grounds that "the test suite can import and drive it" -- until
# these tests existed, nothing did, and a one-word mutant
# (`for path in sorted(args.files)`) reordered the manifest AHEAD of the dump,
# destroying the completion-marker property the whole design rests on, while
# every ordering fence over the .j2 stayed green.
# ---------------------------------------------------------------------------
import importlib.util
import types


def _load_uploader():
    path = _p(f"{BACKUPS_ROLE}/files/mysql-backup-upload.py")
    spec = importlib.util.spec_from_file_location("mysql_backup_upload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _put_object_kwargs() -> dict:
    """Static keyword arguments of the put_object call, read from the AST."""
    import ast as _ast
    tree = _ast.parse(_p(f"{BACKUPS_ROLE}/files/mysql-backup-upload.py").read_text())
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "put_object"):
            out = {}
            for kw in node.keywords:
                try:
                    out[kw.arg] = _ast.literal_eval(kw.value)
                except ValueError:
                    out[kw.arg] = "<dynamic>"
            return out
    raise AssertionError("the uploader no longer calls put_object at all.")


class _FakeS3:
    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        kwargs.pop("Body", None)
        self.calls.append(kwargs)
        return {}


def _drive_uploader(monkeypatch, tmp_path, filenames):
    module = _load_uploader()
    fake = _FakeS3()
    boto3_stub = types.SimpleNamespace(client=lambda *a, **k: fake)
    monkeypatch.setattr(module, "_load_boto3", lambda: True)
    monkeypatch.setattr(module, "boto3", boto3_stub, raising=False)

    paths = []
    for name in filenames:
        f = tmp_path / name
        f.write_bytes(b"payload")
        paths.append(str(f))

    rc = module.main([
        "--bucket", "B", "--kms-key-id", "arn:aws:kms:eu-central-1:1:key/k",
        "--region", "eu-central-1", "--prefix", "pfv-data-01/2026/08/28", *paths,
    ])
    return rc, fake


def test_the_uploader_preserves_argument_order_so_the_manifest_lands_last(
    monkeypatch, tmp_path
):
    rc, fake = _drive_uploader(
        monkeypatch, tmp_path,
        ["pfv2_x.sql.gz", "grants_x.sql.gz", "manifest_x.json"],
    )
    assert rc == 0
    keys = [c["Key"].rsplit("/", 1)[-1] for c in fake.calls]
    assert keys == ["pfv2_x.sql.gz", "grants_x.sql.gz", "manifest_x.json"], (
        f"uploaded in the order {keys}. The caller puts the manifest last "
        "deliberately -- it is the completion marker, and S3 has no rename. "
        "Sorting or reordering here silently destroys that property."
    )


def test_the_uploader_sends_encryption_and_checksum_on_every_object(
    monkeypatch, tmp_path
):
    _, fake = _drive_uploader(monkeypatch, tmp_path, ["a.gz", "b.gz"])
    assert fake.calls, "nothing was uploaded."
    for call in fake.calls:
        assert call["ChecksumAlgorithm"] == "SHA256"
        assert call["ServerSideEncryption"] == "aws:kms"
        assert call["SSEKMSKeyId"].startswith("arn:aws:kms:")


def test_the_uploader_refuses_a_missing_file_before_touching_s3(
    monkeypatch, tmp_path
):
    """Exit 2 and NO uploads: a partial batch is worse than none, because the
    manifest could land beside artifacts that were never written."""
    module = _load_uploader()
    fake = _FakeS3()
    monkeypatch.setattr(module, "_load_boto3", lambda: True)
    monkeypatch.setattr(module, "boto3", types.SimpleNamespace(client=lambda *a, **k: fake),
                        raising=False)
    good = tmp_path / "a.gz"
    good.write_bytes(b"x")
    rc = module.main([
        "--bucket", "B", "--kms-key-id", "k", "--region", "r", "--prefix", "p",
        str(good), str(tmp_path / "missing.gz"),
    ])
    assert rc == 2
    assert fake.calls == [], "uploaded despite a missing file in the batch."
