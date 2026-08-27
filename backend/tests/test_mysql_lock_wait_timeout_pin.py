"""Fences for the metadata-lock timeout pin (TBD-416).

The hazard: MySQL's `lock_wait_timeout` default is 31,536,000 seconds -- 365
days. That is not a timeout. A blocked DDL statement does not fail, it waits,
and its PENDING exclusive MDL outranks every new shared request on the same
table, so the whole application queues behind it. Measured on MySQL 8.4.11
(production's exact version) on 2026-08-27: a RENAME TABLE behind one held
metadata lock was still waiting when the probe gave up, having returned no
error at all; the same statement under a bounded timeout failed cleanly, on
time, with errno 1205.

⚠⚠ NOTHING HERE GREPS A WHOLE FILE. `my.cnf.j2`, `gen-rename-sql.sh` and
`mysql-backup.sh.j2` all carry long comments containing the literal strings
these tests assert -- comments explaining the very settings under test. A
whole-file grep is therefore satisfied by the prose documenting the setting's
own absence, the trap that has bitten this repo repeatedly (TBD-433, TBD-434,
TBD-419). Every assertion below parses, strips comments, or EXECUTES.

⚠⚠ NOR DOES ANYTHING HERE ASSERT THAT A STRING EXISTS. An earlier draft did,
and a review killed most of it: `SET_LINE`/`RENAME_LINE` appearing somewhere in
the generator stayed true after the guard's condition was gutted to `if false`,
so two "refusal" tests passed against a script with no refusal in it. The
lesson generalised -- `set -\\w*e` matched `set -eu` and missed that `pipefail`
is the entire guarantee; `"{{" in value` accepted a pin rendered from the WRONG
role variable; a `mv` assertion never checked what was being moved where. Each
is now pinned to the property, not the token.

⚠ What these fences CANNOT see: whether mysqld actually honours the pin. That
is the play's own live-config readback on a converged server, and extending
that readback is itself fenced here (F2/F3). They also cannot see whether the
play has been RUN -- at the time of writing production is still on the 365-day
default until the next converge.
"""

import configparser
import os
import pathlib
import re
import subprocess

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

VARIABLE = "lock_wait_timeout"
ROLE_VAR = "mysql_lock_wait_timeout"

# The two readback tasks, identified by what they REGISTER rather than by the
# shape of their SQL. Keying on "contains @@ and SELECT CONCAT" made any new
# diagnostic query in the role -- e.g. SELECT CONCAT(@@version,...) -- fail
# these fences, an inverse defect that punishes a correct change.
READBACK_REGISTERS = ("mysql_live_config", "mysql_live_config_after")

# Positions in the readback CONCAT that are literals rather than role
# variables, mapped to the literal the expected list must carry.
LITERAL_POSITIONS = {"bind_address": "0.0.0.0", "collation_server": "utf8mb4_0900_ai_ci"}


def _mysql_role() -> pathlib.Path:
    return REPO_ROOT / "infra" / "ansible" / "roles" / "mysql"


def _strip_jinja_comments(text: str) -> str:
    return re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)


def _my_cnf() -> configparser.ConfigParser:
    raw = _strip_jinja_comments((_mysql_role() / "templates" / "my.cnf.j2").read_text())
    parser = configparser.ConfigParser(allow_no_value=True, strict=False)
    parser.read_string(raw)
    return parser


def _defaults() -> dict:
    return yaml.safe_load((_mysql_role() / "defaults" / "main.yml").read_text()) or {}


def _tasks() -> list:
    return yaml.safe_load((_mysql_role() / "tasks" / "main.yml").read_text()) or []


# ⚠ A synthetic key stamped onto children of an error-swallowing ancestor, so a
# fence cannot be defeated by wrapping the task it checks in
# `block: ... ignore_errors: true`. Same reasoning as _flatten in
# test_dataplane_apt_pins.py.
_SWALLOWED = "_tbd_inherited_error_swallowing"


def _iter_tasks(nodes, swallowed=False):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        mine = swallowed or bool(node.get("ignore_errors"))
        enriched = dict(node)
        enriched[_SWALLOWED] = mine
        yield enriched
        for key in ("block", "rescue", "always"):
            if node.get(key):
                yield from _iter_tasks(node[key], mine)


def _readback_commands() -> dict[str, str]:
    found = {}
    for task in _iter_tasks(_tasks()):
        register = task.get("register")
        if register in READBACK_REGISTERS:
            cmd = task.get("ansible.builtin.command") or {}
            if isinstance(cmd, dict):
                found[register] = str(cmd.get("cmd", ""))
    return found


def _expected_config() -> list:
    for task in _iter_tasks(_tasks()):
        fact = task.get("ansible.builtin.set_fact") or {}
        if isinstance(fact, dict) and "mysql_expected_config" in fact:
            return fact["mysql_expected_config"]
    return []


def _assert_tasks_consuming_expected_config() -> list[dict]:
    out = []
    for task in _iter_tasks(_tasks()):
        block = task.get("ansible.builtin.assert") or {}
        if isinstance(block, dict) and "mysql_expected_config" in str(block.get("that", "")):
            out.append(task)
    return out


# ---------------------------------------------------------------------------
# F1. The pin exists, and renders from the RIGHT role variable.
# Kills: the original defect (no pin, 365-day default); a hardcoded literal;
# and a pin rendered from some OTHER role variable, which would make the play
# fail its own assertion against a box it had just configured correctly.
# ---------------------------------------------------------------------------
def test_my_cnf_pins_the_metadata_lock_timeout_from_its_role_variable():
    mysqld = _my_cnf()["mysqld"]
    assert VARIABLE in mysqld, (
        f"{VARIABLE} is not pinned in my.cnf.j2, so the server keeps MySQL's "
        "31536000-second (365-day) default and a blocked DDL hangs forever "
        "instead of failing."
    )
    value = mysqld[VARIABLE]
    names = re.findall(r"\{\{\s*([a-zA-Z_][\w]*)", value)
    assert names, (
        f"{VARIABLE} is set to the literal {value!r}. It must render from "
        f"{ROLE_VAR} so the value the play WRITES and the value it ASSERTS "
        "cannot drift apart."
    )
    assert ROLE_VAR in names, (
        f"{VARIABLE} renders from {names}, not {ROLE_VAR}. The play would "
        f"write one value and assert {ROLE_VAR}, failing against a server it "
        "had just configured correctly."
    )


def test_the_row_lock_timeout_is_left_alone():
    """Kills: 'fixed' the wrong lock class.

    innodb_lock_wait_timeout bounds ROW-lock waits and has nothing to do with
    metadata locks. Pinning it here would silently change every transaction's
    behaviour under contention.
    """
    assert "innodb_lock_wait_timeout" not in _my_cnf()["mysqld"], (
        "innodb_lock_wait_timeout is a ROW-lock timeout and is not what "
        "TBD-416 is about; it must stay at its default of 50."
    )


def test_the_server_pin_is_a_real_bound():
    value = _defaults()[ROLE_VAR]
    assert isinstance(value, int), f"{ROLE_VAR} must be an integer."
    assert value != 31536000, f"{ROLE_VAR} IS the default this ticket removes."
    # Upper bound, not merely "not the default": the template argues 30s is far
    # above any legitimate wait on a 6.7 MB / 50-table dataset. A pin of, say,
    # 31535999 satisfies "not the default" while bounding nothing in practice.
    assert 0 < value <= 300, (
        f"{ROLE_VAR} is {value}s. Anything approaching the old default stops "
        "being a bound: on this dataset a genuine metadata-lock wait is "
        "sub-second, so a multi-minute pin only ever defers an outage."
    )


# ---------------------------------------------------------------------------
# F2/F3. WRITTEN IS NOT APPLIED -- the role says so in capitals. A pin absent
# from the readback is unproven; a readback nothing asserts against is inert.
# ---------------------------------------------------------------------------
def test_both_readbacks_prove_the_pin_on_the_running_server():
    commands = _readback_commands()
    missing = set(READBACK_REGISTERS) - set(commands)
    assert not missing, (
        f"readback task(s) registering {sorted(missing)} were not found; the "
        "shape these fences assume has changed."
    )
    for register, text in commands.items():
        assert f"@@{VARIABLE}" in text, (
            f"the readback registering {register} does not read @@{VARIABLE}. "
            "WRITING THE FILE IS NOT APPLYING THE CONFIG: a pin missing from "
            "the readback is written to disk and never proven read. Note "
            "mysql_live_config_after is the one the assertion consumes, so "
            "updating only the first leaves the assertion blind."
        )


def test_something_actually_asserts_the_readback():
    """Kills: readback reads a value that nothing ever compares.

    F2 only proves the SELECT names the variable. Delete the assert task and
    both readbacks still name it, the expected list still carries it, and
    nothing on the box is ever checked.
    """
    asserts = _assert_tasks_consuming_expected_config()
    assert asserts, (
        "no ansible.builtin.assert consumes mysql_expected_config, so the "
        "live-config readback registers a value nobody reads and the pin is "
        "unproven on the running server."
    )
    for task in asserts:
        assert not task.get("ignore_errors"), (
            "the live-config assertion carries ignore_errors, which turns the "
            "one check that proves the running config into a no-op."
        )
        assert not task[_SWALLOWED], (
            "the live-config assertion sits inside a block that swallows "
            "errors, so it cannot fail the play."
        )


def test_the_readback_and_the_expected_list_correspond_position_by_position():
    """The play joins both with '|' and compares them as ONE STRING,
    positionally. Length agreement is not correspondence: swap two entries in
    mysql_expected_config and the play fails against a CORRECTLY configured
    server -- the inverse defect, which has shipped here before.
    """
    commands = _readback_commands()
    variable_lists = {r: re.findall(r"@@(\w+)", t) for r, t in commands.items()}

    reference = variable_lists[READBACK_REGISTERS[0]]
    for register, names in variable_lists.items():
        assert names == reference, (
            f"readback {register} reads {names}, but "
            f"{READBACK_REGISTERS[0]} reads {reference}. Both are compared "
            "against one expected list, so they must agree exactly, in order."
        )

    expected = _expected_config()
    assert len(reference) == len(expected), (
        f"the readback reads {len(reference)} variables {reference} but "
        f"mysql_expected_config has {len(expected)} entries. Joined with '|' "
        "and compared as one string, so a length mismatch fails the play "
        "against a server that is configured CORRECTLY."
    )

    for index, (server_var, entry) in enumerate(zip(reference, expected)):
        entry = str(entry)
        if server_var in LITERAL_POSITIONS:
            assert LITERAL_POSITIONS[server_var] in entry, (
                f"position {index}: the readback reads @@{server_var} but the "
                f"expected entry is {entry!r}."
            )
            continue
        names = re.findall(r"\{\{[^}]*?\b(mysql_\w+)", entry)
        assert names, (
            f"position {index}: the readback reads @@{server_var} but the "
            f"expected entry {entry!r} names no role variable."
        )
        assert any(server_var in name for name in names), (
            f"position {index} MISALIGNED: the readback reads @@{server_var} "
            f"but the expected entry at that position derives from {names}. "
            "The comparison is positional, so this fails the play against a "
            "correctly configured server."
        )


# ---------------------------------------------------------------------------
# F4/F5. The rename artifact carries its own bound, before the statement it
# bounds, and strictly below the server pin.
# ---------------------------------------------------------------------------
def _generator_path() -> pathlib.Path:
    return REPO_ROOT / "infra" / "ansible" / "bin" / "gen-rename-sql.sh"


def _generator_lines(strip_comments: bool = True) -> list[str]:
    lines = _generator_path().read_text().splitlines()
    if not strip_comments:
        return lines
    return [line for line in lines if not line.lstrip().startswith("#")]


def test_the_rename_generator_emits_the_session_bound_before_the_rename():
    lines = _generator_lines()
    set_at = [i for i, l in enumerate(lines)
              if re.search(r'echo\s+"SET SESSION\s+' + VARIABLE, l)]
    rename_at = [i for i, l in enumerate(lines) if re.search(r'echo\s+"RENAME TABLE', l)]
    assert set_at, (
        f"gen-rename-sql.sh does not EMIT a session {VARIABLE} bound (comment "
        "lines are stripped first, so prose about the setting cannot satisfy "
        "this). Without it the generated rename inherits the 365-day default."
    )
    assert rename_at, "gen-rename-sql.sh no longer emits a RENAME TABLE statement."
    assert min(set_at) < min(rename_at), (
        "the session bound is emitted AFTER the RENAME TABLE it is meant to "
        "bound, which does nothing at all."
    )


def test_the_session_bound_is_strictly_below_the_server_pin():
    """The ordering IS the design. The server pin is also the VICTIM's timeout
    -- what an app query waits while queued behind the rename's pending
    exclusive MDL. Session below global means the rename yields first and the
    queue drains with no user-visible errors. Inverted, real users get 1205
    while the rename keeps waiting.
    """
    global_value = _defaults()[ROLE_VAR]
    session_values = [
        int(m.group(1))
        for l in _generator_lines()
        if (m := re.search(r'echo\s+"SET SESSION\s+' + VARIABLE + r'\s*=\s*(\d+)\s*;', l))
    ]
    assert session_values, "no session bound literal found to compare."
    for value in session_values:
        assert value < global_value, (
            f"the rename's session bound ({value}s) is not strictly below the "
            f"server pin ({global_value}s). With session >= global, app "
            "queries queued behind the rename's pending exclusive MDL time out "
            "and error real users BEFORE the rename gives up."
        )


def test_the_refusal_guard_runs_before_the_artifact_is_emitted():
    """Kills: a guard that fires after `cat "$OUT"`.

    By then the artifact is already on stdout and, in the runbook, already in
    the operator's file. Refusing afterwards refuses nothing.
    """
    lines = _generator_lines()
    guard_at = next((i for i, l in enumerate(lines) if l.startswith("SET_LINE=")), None)
    emit_at = next((i for i, l in enumerate(lines) if re.match(r'\s*cat\s+"\$OUT"', l)), None)
    assert guard_at is not None, "the refusal guard's SET_LINE= assignment is gone."
    assert emit_at is not None, "gen-rename-sql.sh no longer emits the artifact."
    assert guard_at < emit_at, (
        "the refusal guard runs AFTER the artifact is written to stdout, so a "
        "rejected artifact has already been produced and piped."
    )


# ---------------------------------------------------------------------------
# F5b. THE GUARD IS EXECUTED, NOT PATTERN-MATCHED -- and executed in the shell
# mode the real script uses.
# ---------------------------------------------------------------------------
GUARD_SENTINEL = "lock_wait_timeout bound missing"

# ⚠ Fixtures mirror the REAL artifact, which opens with two `--` comment lines.
# Without them a guard whose greps lost their `^` anchors passes every test,
# and would then be satisfiable by a commented-out SET line inside the file --
# this module's own headline trap, on the one surface whose input is written by
# hand rather than parsed.
HEADER = "-- TBD-360 Phase 2: pfv2 -> tbd. Generated 2026-08-27T00:00:00Z.\n-- 50 base tables, renamed in ONE atomic statement.\n"
BOUNDED = HEADER + "SET SESSION lock_wait_timeout = 10;\nRENAME TABLE\n  a.`t` TO b.`t`;\n"
UNBOUNDED = HEADER + "RENAME TABLE\n  a.`t` TO b.`t`;\n"
INVERTED = HEADER + "RENAME TABLE\n  a.`t` TO b.`t`;\nSET SESSION lock_wait_timeout = 10;\n"
COMMENTED_OUT = HEADER + "-- SET SESSION lock_wait_timeout = 10;\nRENAME TABLE\n  a.`t` TO b.`t`;\n"


def _extract_guard() -> str:
    """Lift the refusal guard out of gen-rename-sql.sh so it can be RUN.

    ⚠ The end boundary is found by if/fi DEPTH, not by 'the next line that is
    `fi`'. A naive scan runs straight past a deleted guard into the NEXT
    if-block (the pair-count truncation check), whose `exit 1` then fires on an
    undefined variable -- so both refusal tests would report "refusal proven"
    while executing an unrelated check against a script with no guard in it.

    ⚠ The harness reproduces the script's own `set -euo pipefail`. Without it
    the guard is executed in a shell mode production never uses, which is how
    the dead-code defect below hid: a non-matching grep exits 1, pipefail
    propagates it to the assignment, and `set -e` kills the script BEFORE the
    `if`, printing nothing. The `|| true` on those assignments is what keeps
    the diagnostics reachable, and this harness is what holds it there.
    """
    lines = _generator_path().read_text().splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("SET_LINE=")), None)
    if start is None:
        pytest.fail(
            "could not locate the refusal guard in gen-rename-sql.sh: no line "
            "begins with `SET_LINE=`. If the guard was refactored (e.g. into a "
            "shell function), update _extract_guard rather than deleting this."
        )
    depth, end, saw_if = 0, None, False
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if re.match(r"^if\b|^if\[", stripped):
            depth += 1
            saw_if = True
        elif stripped == "fi":
            depth -= 1
            if depth == 0 and saw_if:
                end = i
                break
    if end is None:
        pytest.fail("could not find the end of the refusal guard's if-block.")

    body = "\n".join(lines[start:end + 1])
    condition = "\n".join(l for l in lines[start:end + 1] if re.match(r"^\s*if\b", l))
    assert "SET_LINE" in condition and "RENAME_LINE" in condition, (
        "the extracted if-block does not compare SET_LINE against "
        f"RENAME_LINE; it reads:\n{condition}\nThe guard has been removed or "
        "gutted, and what was extracted is a DIFFERENT check."
    )
    return "\n".join(["set -euo pipefail", 'OUT="$1"', body, 'echo GUARD_PASSED'])


def _run_guard(artifact: str, tmp_path):
    guard = tmp_path / "guard.sh"
    guard.write_text(_extract_guard())
    out = tmp_path / "rename.sql"
    out.write_text(artifact)
    return subprocess.run(["bash", str(guard), str(out)], capture_output=True, text=True)


def test_the_generator_accepts_a_correctly_bounded_artifact(tmp_path):
    """The inverse defect: a guard that refuses everything is not a guard."""
    result = _run_guard(BOUNDED, tmp_path)
    assert result.returncode == 0, (
        "the refusal guard rejects a CORRECTLY bounded rename artifact, so it "
        f"would refuse to generate the real one.\nstderr: {result.stderr}"
    )
    assert "GUARD_PASSED" in result.stdout


@pytest.mark.parametrize(
    "artifact, why",
    [
        (UNBOUNDED, "no session bound at all -- inherits the 365-day default"),
        (INVERTED, "bound emitted AFTER the RENAME it should bound"),
        (COMMENTED_OUT, "bound present only as a SQL comment"),
    ],
    ids=["unbounded", "bound-too-late", "bound-only-commented-out"],
)
def test_the_generator_refuses_an_unsafe_artifact(artifact, why, tmp_path):
    """⚠ Asserts the EXIT CODE *and* the guard's own message.

    Accepting any non-zero is what let a gutted guard pass: an unrelated check
    failing on an undefined variable also exits non-zero. The sentinel proves
    which code refused.
    """
    result = _run_guard(artifact, tmp_path)
    assert result.returncode == 1, (
        f"gen-rename-sql.sh does not refuse an artifact with {why}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert GUARD_SENTINEL in result.stderr, (
        f"the guard refused an artifact with {why}, but did not print its own "
        "diagnostic -- so it failed for some other reason, or died before the "
        "`if` could run. Under `set -euo pipefail` a non-matching grep kills "
        "the assignment unless it carries `|| true`.\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# F6. THE ARTIFACT-CLASS FENCE. These live with the pin because the pin ARMS
# them. Measured 2026-08-27 on the real pipeline shape:
#   producer HANGS (the 365-day default) -> gzip never sees EOF, never writes
#       the CRC32/ISIZE trailer; `gzip -t` FAILS, "unexpected end of file".
#       The corrupt artifact announces itself.
#   producer EXITS NONZERO mid-stream (errno 1205, reachable only once the pin
#       exists) -> gzip finalizes; `gzip -t` PASSES on 55,953 lines of valid
#       SQL with no completion marker.
# Bounding the wait converts a self-announcing corrupt backup into a silent one,
# against what TBD-399/TBD-400 record as the ONLY backup.
# ---------------------------------------------------------------------------
def _backup_path() -> pathlib.Path:
    return (REPO_ROOT / "infra" / "ansible" / "roles" / "backups"
            / "templates" / "mysql-backup.sh.j2")


def _backup_lines() -> list[str]:
    return [l for l in _backup_path().read_text().splitlines()
            if not l.lstrip().startswith("#")]


def test_the_nightly_dump_never_streams_to_its_final_name():
    lines = _backup_lines()
    redirects = [l for l in lines if re.search(r"\|\s*gzip\s*>", l)]
    assert redirects, "the dump no longer pipes through gzip; this fence is stale."
    for line in redirects:
        target = re.search(r"\|\s*gzip\s*>\s*(\S+)", line).group(1).strip('"')
        assert target != "${DUMP}", (
            "the nightly dump streams straight to its FINAL name. A dump that "
            "dies mid-stream then leaves a structurally valid gzip of "
            "truncated SQL there -- `gzip -t` passes, the size is plausible, "
            "and the only tell is a missing completion marker."
        )


def test_pipefail_is_what_stops_a_failed_dump_being_published():
    """⚠ `set -e` ALONE IS NOT ENOUGH, and asserting it is the trap.

    `mysqldump ... | gzip > "${DUMP}.part"` makes mysqldump the LEFT side of a
    pipe. Without `pipefail` the pipeline's status is gzip's, which succeeds,
    so a 1205 from mysqldump is masked, the script continues, and `mv`
    publishes the truncated dump under the final name -- the exact artifact
    this whole block exists to prevent. A `set -\\w*e` regex matches `set -eu`
    and lets that mutant through.
    """
    lines = _backup_lines()
    set_at = [i for i, l in enumerate(lines) if re.match(r"\s*set\s+-", l)]
    assert set_at, "the backup script sets no shell options at all."
    options = " ".join(lines[i] for i in set_at)
    assert "pipefail" in options, (
        "the backup script does not set `pipefail`. mysqldump is the left side "
        "of a pipe, so without it a failed dump is masked by gzip's success "
        "and the truncated result is renamed into place as though it were good."
    )
    assert re.search(r"set\s+-\w*e", options), (
        "the backup script does not set `-e`, so a failed step does not abort."
    )
    first_pipeline = next(i for i, l in enumerate(lines) if re.search(r"\|\s*gzip\s*>", l))
    assert min(set_at) < first_pipeline, (
        "the shell options are set AFTER the dump pipeline, so they do not "
        "govern it."
    )


def test_the_dump_is_renamed_from_the_temporary_name_to_the_final_one():
    """Kills: an `mv` that moves the wrong thing, or to the wrong place.

    Asserting merely that SOME `mv` exists let `mv "${DUMP}.part"
    "${DUMP}.part2"` pass -- after which a successful run never produces a file
    at the final name at all.
    """
    lines = _backup_lines()
    dump_at = next(i for i, l in enumerate(lines) if re.search(r"\|\s*gzip\s*>", l))
    source = re.search(r"\|\s*gzip\s*>\s*(\S+)", lines[dump_at]).group(1).strip('"')

    movers = [(i, l) for i, l in enumerate(lines) if re.match(r"\s*mv\s+", l)]
    assert movers, (
        "nothing renames the temporary dump into place, so a successful run "
        "never produces a file at the final name."
    )
    index, line = movers[0]
    assert index > dump_at, "the rename does not follow the dump."
    operands = re.findall(r'"([^"]+)"', line)
    assert len(operands) == 2, f"could not read the mv operands from {line!r}."
    src, dst = operands
    assert src == source, (
        f"the rename moves {src!r}, but the dump was written to {source!r}."
    )
    assert dst == "${DUMP}", (
        f"the rename publishes to {dst!r}, not the final name ${{DUMP}}. A "
        "successful run would leave nothing where the backup is expected."
    )


def test_the_cleanup_trap_removes_the_TEMPORARY_file_and_not_the_backup():
    """Kills: `trap 'rm -f "${DUMP}"' EXIT`.

    That fires on the SUCCESS path too -- after `mv` has put the completed dump
    at the final name -- and so deletes the finished nightly backup every
    night, against what this script calls the only backup there is.
    """
    body = "\n".join(_backup_lines())
    traps = re.findall(r"trap\s+'([^']*)'\s+(\w+)", body)
    assert traps, (
        "no trap cleans up the temporary file, so a failed night leaves an "
        "orphan behind."
    )
    cleanup = [(cmd, sig) for cmd, sig in traps if "rm" in cmd]
    assert cleanup, "no trap removes anything."
    for command, signal in cleanup:
        assert signal == "EXIT", f"the cleanup trap is on {signal}, not EXIT."
        targets = re.findall(r'"([^"]+)"', command)
        assert targets, f"could not read the trap's target from {command!r}."
        for target in targets:
            assert target.endswith(".part"), (
                f"the cleanup trap removes {target!r}. On the success path the "
                "dump has already been renamed to the final name, so a trap "
                "aimed at ${DUMP} deletes the COMPLETED backup on every "
                "successful run."
            )


def test_retention_can_reap_a_temporary_file_left_by_a_kill():
    """The EXIT trap does not run on SIGKILL, an OOM kill, or a reboot -- and
    this is a 2 GB box running a 768M buffer pool alongside Redis. A surviving
    `.part` is dump-sized, so retention has to be able to see it. A glob
    ending `.sql.gz` cannot, which would make the orphan permanent: a
    REGRESSION against the old code, whose partial sat at the final name where
    retention still matched it.
    """
    body = "\n".join(_backup_lines())
    finds = [l for l in _backup_lines() if l.strip().startswith("find")]
    assert finds, "retention no longer runs."
    globs = re.findall(r'-name\s+"([^"]+)"', " ".join(finds))
    assert globs, "the retention find has no -name glob."
    assert any(not g.endswith(".sql.gz") for g in globs), (
        f"the retention globs are {globs}. None can match a "
        "`.sql.gz.part` orphan left by a SIGKILL, so such a file would never "
        "be reaped -- a dump-sized permanent leak on the single node."
    )
    assert "-mtime" in body, (
        "retention no longer age-gates deletion, so it could delete a dump "
        "written moments ago."
    )


def test_the_trap_is_disarmed_before_the_rename_into_place():
    """Kills: leaving the EXIT trap armed across the `mv`.

    At the `mv` the dump is complete and valid. If the `mv` itself fails --
    EIO, a read-only filesystem -- `set -e` aborts with the trap still armed,
    and the trap then deletes `${DUMP}.part`: a finished backup that the old
    `gzip > "${DUMP}"` code would have left on disk. The trap's blast radius
    has to stop at the failure path it was written for.
    """
    lines = _backup_lines()
    disarm_at = next((i for i, l in enumerate(lines)
                      if re.match(r"\s*trap\s+-\s+EXIT\s*$", l)), None)
    mv_at = next((i for i, l in enumerate(lines) if re.match(r"\s*mv\s+", l)), None)
    assert mv_at is not None, "nothing renames the dump into place."
    assert disarm_at is not None, (
        "the cleanup trap is never disarmed, so it stays armed across the "
        "`mv`. A failing `mv` would then delete a COMPLETE dump -- strictly "
        "worse than the old behaviour, which left it on disk."
    )
    assert disarm_at < mv_at, (
        "the trap is disarmed AFTER the `mv`, which is too late: if the `mv` "
        "fails, `set -e` aborts before the disarm and the trap eats the "
        "finished backup."
    )
