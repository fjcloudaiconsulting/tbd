from app.services.scheduler.base import JobResult, OUTCOME_NOOP, OUTCOME_SUCCESS


def test_jobresult_helpers():
    assert JobResult.noop().outcome == OUTCOME_NOOP
    ok = JobResult.ok({"generated": 2})
    assert ok.outcome == OUTCOME_SUCCESS
    assert ok.counts == {"generated": 2}
    assert ok.error is None
    bad = JobResult.failed("boom")
    assert bad.outcome == "failure"
    assert bad.error == "boom"
