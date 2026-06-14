"""Per-source validity is enforced after Pydantic parse and surfaces as 422."""
import pytest

from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation, Dataset, Dimension, Filter, FilterField, FilterOp,
    Measure, MeasureField, ReportsQuery,
)


def _q(dataset, measure, dims=None, filters=None):
    return ReportsQuery(
        dataset=dataset, measure=measure,
        dimensions=dims or [], filters=filters or [],
    )


def test_transactions_rejects_balance_measure():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS, Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE))
    with pytest.raises(ValueError):
        src.validate(q)


def test_transactions_rejects_currency_dimension():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           dims=[Dimension.CURRENCY])
    with pytest.raises(ValueError):
        src.validate(q)


def test_transactions_accepts_its_own_surface():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           dims=[Dimension.CATEGORY])
    src.validate(q)  # no raise
