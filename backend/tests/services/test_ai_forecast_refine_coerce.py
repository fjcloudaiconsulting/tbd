"""LAI.2 — the model's structured output is non-deterministic; validation must
sanitize/clamp it instead of rejecting the whole response on one stray field.

Reproduces the prod failure (`ai_response_invalid_schema`, error_count ~= #cats):
the dispatch succeeds but strict Pydantic rejected ~1 field per seasonal row,
nuking the entire refinement to baseline.
"""
from app.schemas.ai_forecast import AIForecastAdjustments
from app.services.ai_forecast_refine_service import _coerce_adjustments


def test_coerce_clamps_out_of_range_and_truncates_then_validates():
    # A realistic "misbehaving" model response that the OLD strict validation
    # would reject wholesale.
    parsed = {
        "seasonal": [
            # multiplier above the 1.5 cap -> clamp, not reject
            {"category_id": 1, "category_name": "Rent", "multiplier": 1.8,
             "rationale": "x" * 500},                       # over 240 chars
            # multiplier below 0.5 -> clamp up
            {"category_id": "2", "category_name": "Food", "multiplier": 0.1,
             "rationale": "down"},                          # category_id as str
        ],
        "anomalies": [
            {"category_id": 3, "category_name": "Travel",
             "description": "y" * 500, "severity": "critical"},  # bad severity + long
        ],
        "confidence": 2.0,                                  # above 1.0
        "summary": "z" * 1000,                              # over 480
    }

    coerced = _coerce_adjustments(parsed)

    # The coerced dict must pass the strict model (no fallback).
    adj = AIForecastAdjustments.model_validate(coerced)

    assert adj.seasonal[0].multiplier == 1.5               # clamped down
    assert adj.seasonal[1].multiplier == 0.5               # clamped up
    assert adj.seasonal[1].category_id == 2                # coerced str->int
    assert len(adj.seasonal[0].rationale) <= 240
    assert adj.anomalies[0].severity == "info"             # bad sev -> default
    assert len(adj.anomalies[0].description) <= 240
    assert adj.confidence == 1.0                            # clamped
    assert len(adj.summary) <= 480


def test_coerce_drops_unusable_rows_but_keeps_good_ones():
    parsed = {
        "seasonal": [
            {"category_name": "NoId", "multiplier": 1.0, "rationale": "r"},   # missing category_id -> drop
            {"category_id": 9, "category_name": "Keep", "multiplier": 1.1, "rationale": "r"},
            "not-a-dict",                                                     # junk -> drop
        ],
        "anomalies": [],
        "confidence": 0.7,
        "summary": "ok",
    }
    coerced = _coerce_adjustments(parsed)
    adj = AIForecastAdjustments.model_validate(coerced)
    assert [s.category_id for s in adj.seasonal] == [9]


def test_coerce_truncates_lists_to_schema_caps():
    # A model returning more rows than the schema allows must NOT trigger a
    # full fallback; coercion truncates to the AIForecastAdjustments caps.
    parsed = {
        "seasonal": [
            {"category_id": i, "category_name": f"C{i}", "multiplier": 1.0, "rationale": "r"}
            for i in range(1, 260)  # 259 > 200 cap
        ],
        "anomalies": [
            {"category_id": i, "category_name": f"C{i}", "description": "d", "severity": "info"}
            for i in range(1, 120)  # 119 > 60 cap
        ],
        "confidence": 0.5,
        "summary": "ok",
    }
    coerced = _coerce_adjustments(parsed)
    adj = AIForecastAdjustments.model_validate(coerced)  # must not raise
    assert len(adj.seasonal) == 200
    assert len(adj.anomalies) == 60


def test_coerce_maps_non_finite_numbers_to_neutral_default():
    # nan/inf from the model must become the neutral default (multiplier 1.0,
    # confidence 0.5), not be absorbed to a clamp bound (e.g. 1.5).
    for bad in ("nan", "inf", "-inf"):
        parsed = {
            "seasonal": [{"category_id": 1, "category_name": "A", "multiplier": bad, "rationale": "r"}],
            "anomalies": [],
            "confidence": bad,
            "summary": "ok",
        }
        adj = AIForecastAdjustments.model_validate(_coerce_adjustments(parsed))
        assert adj.seasonal[0].multiplier == 1.0
        assert adj.confidence == 0.5


def test_coerce_supplies_defaults_for_missing_required_fields():
    # Model omits confidence/summary/rationale entirely.
    parsed = {"seasonal": [{"category_id": 1, "category_name": "A", "multiplier": 1.0}]}
    coerced = _coerce_adjustments(parsed)
    adj = AIForecastAdjustments.model_validate(coerced)
    assert adj.seasonal[0].rationale == ""
    assert 0.0 <= adj.confidence <= 1.0
    assert isinstance(adj.summary, str)
