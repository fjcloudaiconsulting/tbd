"""Fences for the off-host MySQL backup (TBD-400).

The hazard: the nightly dump was written to /var/backups/mysql on the droplet it
protects, with droplet snapshots off, so a disk or droplet loss took the data and
its only backup together. Fixing that adds three things that can each fail
silently -- a verification, an upload, and an alarm -- so each is fenced by being
EXECUTED, not by being read. The alarm (the off-host freshness probe) and its
tests live in fjcloudaiconsulting/aws-infra since INFRA-20.

⚠⚠ THE VERIFICATION IS A REAL FILE, NOT JINJA, SPECIFICALLY SO THESE TESTS CAN
RUN IT. Logic embedded in a `.j2` can only ever be grep-fenced, and in this repo a grep is routinely satisfied by a comment -- the
script carries long comments naming the very strings a grep would look for.

⚠ A behavioural test found a real defect here that a structural one could not:
the freshness probe's `check-backup-freshness.sh` originally ran its evaluator
as `python3 - <<'PY'`, which makes the HEREDOC stdin, so the piped S3 listing never reached the program
and every input, healthy or not, was judged "listing has no Contents key". A
fence asserting the script mentions "Contents" would have passed it.
"""

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
# F3. The uploader is put-only, and stays that way.
# ---------------------------------------------------------------------------
# The policy/trust documents moved to aws-infra, whose CI runs
# .github/scripts/check-tbd-backups-fences.py (exact uploader/probe action sets,
# Allow-only + bucket/key-scoped resources, SSE condition, F5 TBD-372 trust
# anchor incl. no-glob). Only the uploader script (still here) is checked.


def test_the_uploader_must_name_the_encryption_key_explicitly():
    """The policy conditions on the SSE headers with StringEquals, and
    StringEquals against an ABSENT header FAILS. Relying on the bucket default
    does not satisfy it, and the resulting 403 reads like a credential problem.
    So the uploader has to send both, explicitly."""
    # backup-uploader.json (aws-infra terraform/tbd-backups/policies/) conditions
    # on s3:x-amz-server-side-encryption == aws:kms and the kms-key-id header.
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
