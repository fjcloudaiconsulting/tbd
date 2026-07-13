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


def test_transactions_accepts_status_eq():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           filters=[Filter(field=FilterField.STATUS, op=FilterOp.EQ, value="settled")])
    src.validate(q)  # no raise — transactions publishes status (eq)


def test_transactions_rejects_status_non_eq_op():
    # ``status`` is now a shared-canvas field, but transactions publishes it
    # as eq-only; a non-eq op must still 422 at validate (not be silently
    # dropped by the shared-canvas branch), so the published-field op-check
    # stays intact.
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           filters=[Filter(field=FilterField.STATUS, op=FilterOp.IN,
                           value=["settled", "pending"])])
    with pytest.raises(ValueError):
        src.validate(q)
