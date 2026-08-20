#!/usr/bin/env bash
# Decide whether one upstream job's result may pass a required aggregate gate.
#
#   assert-gate.sh <job-result> <area-changed> <label>
#
# Exit 0 ONLY when:
#   * <job-result> is exactly `success`, or
#   * <job-result> is exactly `skipped` AND <area-changed> is exactly `false`.
# Everything else exits 1.
#
# WHY THIS EXISTS (TBD-404)
#
# `test.yml` scopes its work jobs to the area they test, so a docs-only PR
# skips the six backend shards, `Migration Checks` and the frontend suite. The
# two required contexts (`Backend Checks`, `Frontend Checks`) still run — they
# must, because a required context that never reports blocks its PR forever —
# and they therefore have to accept a `skipped` upstream.
#
# ⚠ THE TRAP THIS SCRIPT EXISTS TO CLOSE. GitHub reports `skipped` for BOTH of
# these, indistinguishably from the aggregate's point of view:
#
#   (a) the job's own `if:` was false            -> nothing to test, fine
#   (b) an UPSTREAM job failed or was cancelled  -> the suite is broken
#
# So a blanket `result == "skipped" -> pass` turns a genuinely red suite into a
# green REQUIRED gate, on the exact gate that exists to stop a red suite
# merging. The accept is therefore conditional on the change-detection output
# for that area saying it did not change, and on nothing else.
#
# ⚠ `<area-changed>` MUST BE EXACTLY `false`, NOT merely "not true". If the
# `changes` job itself fails or is cancelled, `needs.changes.outputs.<area>`
# evaluates to the EMPTY STRING; every work job's `if:` is then false, every
# one of them reports `skipped`, and a `!= "true"` rule would wave the entire
# suite through. Requiring the literal `false` means the gate can only be
# satisfied by change detection that actually ran and actually answered.
# (The aggregates additionally assert `needs.changes.result` through this same
# script, so that path fails twice over. Both are deliberate.)
#
# ⚠ A `failure` is NEVER excused by "the area did not change". If a job ran at
# all, its verdict is the answer; the change-detection output is only ever
# allowed to explain an absence, never to overrule a result.
#
# Fenced by backend/tests/test_ci_gate_accept_rule.py, which drives THIS file
# over the full truth table. Do not re-implement the rule inline in the
# workflow: one implementation, one fence.
set -uo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: assert-gate.sh <job-result> <area-changed> <label>" >&2
  exit 2
fi

result="$1"
changed="$2"
label="$3"

if [ "$result" = "success" ]; then
  echo "${label}: passed."
  exit 0
fi

if [ "$result" = "skipped" ] && [ "$changed" = "false" ]; then
  echo "${label}: skipped -- change detection reported no changes in this area."
  exit 0
fi

if [ "$result" = "skipped" ]; then
  echo "${label}: reported 'skipped' but change detection did NOT report" >&2
  echo "'false' for this area (it reported '${changed}')." >&2
  echo "A job also reports 'skipped' when an upstream job failed or was" >&2
  echo "cancelled, so this cannot be waved through." >&2
  exit 1
fi

echo "${label}: failed with result '${result}' (area-changed='${changed}')." >&2
exit 1
