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

GOOD_GRANTS = (
    b"-- grants\n"
    b"CREATE USER 'pfv_app'@'%' IDENTIFIED WITH 'caching_sha2_password' AS '$A$005$x';\n"
    b"GRANT ALL ON pfv2.* TO 'pfv_app'@'%';\n"
)
GRANTS_NO_APP = b"-- grants\nGRANT USAGE ON *.* TO 'someone'@'%';\n"


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


def test_the_table_count_is_never_hardcoded_in_the_backup_script():
    """Kills: a literal count. 50 is a measurement of today's schema, not an
    invariant, so a literal turns the next migration into a red check against a
    perfectly good backup -- a wall-clock date bomb in a different costume."""
    body = "\n".join(_script_lines(f"{BACKUPS_ROLE}/templates/mysql-backup.sh.j2"))
    assert "information_schema.tables" in body, (
        "the backup script no longer reads the table count live."
    )
    assert not re.search(r"EXPECTED_TABLES=\s*['\"]?\d+", body), (
        "the expected table count is hardcoded in the backup script."
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
    "payload", ["not json", '{"KeyCount": 0}', ""],
    ids=["malformed", "no-contents-key", "empty-stdin"],
)
def test_probe_reports_could_not_run_rather_than_healthy(payload):
    """⚠ Exit 2, never 0. A probe that cannot answer must not be mistaken for a
    probe that answered 'fine' -- and an ABSENT Contents key means the listing
    failed, which is not the same as an empty bucket."""
    r = _probe(payload)
    assert r.returncode == 2, f"rc={r.returncode} out={r.stdout}"


def test_the_probe_workflow_alarms_on_could_not_run_too():
    """Kills: only failing the job. A red scheduled workflow notifies nobody,
    and nothing else in this repo covers this signal."""
    import yaml
    wf = yaml.safe_load(_p(".github/workflows/backup-freshness-probe.yml").read_text())
    steps = wf["jobs"]["probe"]["steps"]
    alarm = [s for s in steps if "notify-backup-stale.sh" in str(s.get("run", ""))]
    assert alarm, "the workflow never invokes the alarm script."
    cond = str(alarm[0].get("if", ""))
    assert "!=" in cond and "fresh" in cond, (
        f"the alarm fires on {cond!r}. It must fire on anything that is not "
        "'fresh', so a could-not-run verdict raises the alarm rather than "
        "silently failing the job."
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

    src = _p(f"{BACKUPS_ROLE}/files/mysql-backup-upload.py").read_text()
    assert "ServerSideEncryption" in src and "SSEKMSKeyId" in src, (
        "the uploader does not send the SSE parameters the IAM policy requires."
    )
    assert 'ChecksumAlgorithm="SHA256"' in src, (
        "the uploader does not request a SHA-256 checksum. That checksum IS the "
        "transport verification: S3 recomputes it server-side and rejects a "
        "mismatch, which is how the uploaded object is verified WITHOUT read "
        "permission."
    )


# ---------------------------------------------------------------------------
# F4. Ordering inside the backup script.
# ---------------------------------------------------------------------------
def test_nothing_is_published_or_uploaded_before_it_is_verified():
    lines = _script_lines(f"{BACKUPS_ROLE}/templates/mysql-backup.sh.j2")
    def first(pattern):
        return next((i for i, ln in enumerate(lines) if re.search(pattern, ln)), None)

    verify = first(r"mysql_backup_verify_script")
    mv = first(r"^\s*mv\s+")
    upload = first(r"mysql_backup_upload_script")

    assert verify is not None, "the backup script never calls the verifier."
    assert mv is not None and upload is not None
    assert verify < mv, (
        "the dump is renamed into place BEFORE it is verified, so a bad dump is "
        "published at the final name."
    )
    assert verify < upload, (
        "the dump is uploaded BEFORE it is verified, which would replicate a "
        "corrupt artifact off-host and mark it verified."
    )


def test_the_manifest_is_uploaded_last():
    """S3 has no rename, so the .part trick does not lift. The manifest's
    presence is the completion marker; uploading it first would mark a night
    complete before its artifacts existed."""
    body = "\n".join(_script_lines(f"{BACKUPS_ROLE}/templates/mysql-backup.sh.j2"))
    call = re.search(r"mysql_backup_upload_script.*?\n\n", body, re.DOTALL)
    assert call, "could not locate the upload invocation."
    args = call.group(0)
    assert args.index("${DUMP}") < args.index("${MANIFEST}")
    assert args.index("${GRANTS}") < args.index("${MANIFEST}")


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
    for sub in subs:
        assert f"workspace:{workspace}:" in sub, (
            f"versions.tf declares workspace {workspace!r} but the committed "
            f"trust document authorizes {sub!r}. Applying this would deny the "
            "workspace its own role and it could not apply the fix (TBD-372). "
            "Widen the pattern, apply, rename, then narrow."
        )


def test_the_trust_document_does_not_glob_the_workspace_name():
    """apex carries `tbd-apex*` as scar tissue from the rename that caused
    TBD-372. A fresh anchor should not inherit a wildcard on the segment that IS
    the trust boundary."""
    trust = _p("infra/aws/bootstrap/tfc-backups-trust.json").read_text()
    for sub in re.findall(r'"app\.terraform\.io:sub":\s*"([^"]+)"', trust):
        seg = re.search(r"workspace:([^:]+):", sub)
        assert seg and "*" not in seg.group(1), (
            f"the workspace segment of {sub!r} is globbed; it must be exact."
        )
