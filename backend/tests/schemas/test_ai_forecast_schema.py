import pytest
from pydantic import ValidationError
from app.schemas.ai_forecast import RefineForecastRequest, ForecastRefineEstimate


def test_request_defaults_are_6_months_top_20():
    req = RefineForecastRequest()
    assert req.timeframe_months == 6
    assert req.scope == "top_20"


def test_request_rejects_bad_timeframe_and_scope():
    with pytest.raises(ValidationError):
        RefineForecastRequest(timeframe_months=7)
    with pytest.raises(ValidationError):
        RefineForecastRequest(scope="everything")


def test_estimate_response_shape():
    est = ForecastRefineEstimate(
        est_prompt_tokens=11000,
        est_output_tokens=2000,
        est_cost_cents=15,
        duration_band="~20-40s",
        can_proceed=True,
        reason=None,
    )
    assert est.can_proceed is True
