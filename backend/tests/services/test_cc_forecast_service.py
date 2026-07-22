"""Pure-math unit tests for cc_forecast_service (Credit Card Model V1, Slice 3).

DB-free. Proves the locked forecast invariants at the arithmetic level:
carried-balance-as-of-close B_k, the unified clamp+net resolver, S_prev
and consumed-credit threading, and every strategy's target.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services import cc_forecast_service as svc


class _FakeAccount:
    """Stand-in exposing exactly the attributes the pure math reads."""

    def __init__(
        self,
        *,
        id=1,
        close_day=25,
        payment_day=1,
        payment_day_relative_month=1,
        payment_strategy="full_balance",
        fixed_payment_amount=None,
        currency="EUR",
    ):
        self.id = id
        self.close_day = close_day
        self.payment_day = payment_day
        self.payment_day_relative_month = payment_day_relative_month
        self.payment_strategy = payment_strategy
        self.fixed_payment_amount = fixed_payment_amount
        self.currency = currency


P_START = date(2026, 5, 1)
P_END = date(2026, 5, 31)


def test_due_cycles_in_horizon_grace_period_close_in_past():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(acct, p_start=P_START, p_end=P_END)
    assert len(cycles) == 1
    assert cycles[0].period_end_inclusive == date(2026, 4, 25)
    assert cycles[0].payment_date == date(2026, 5, 1)


def test_due_cycles_in_horizon_two_due_dates():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30)
    )
    assert [c.payment_date for c in cycles] == [date(2026, 5, 1), date(2026, 6, 1)]


def test_due_cycles_in_horizon_none_when_no_due_date():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(
        acct, p_start=date(2026, 5, 10), p_end=date(2026, 5, 15)
    )
    assert cycles == []


def test_balance_at_close_excludes_activity_after_close():
    ledger = [
        (date(2026, 4, 10), Decimal("-500.00")),   # before close -> counts
        (date(2026, 5, 3), Decimal("-700.00")),     # after close  -> excluded
    ]
    b_k = svc.balance_at_close(Decimal("0.00"), ledger, date(2026, 4, 25))
    assert b_k == Decimal("-500.00")
    assert svc.outstanding_at_close(b_k) == Decimal("500.00")


def test_outstanding_at_close_card_in_credit_is_zero():
    assert svc.outstanding_at_close(Decimal("120.00")) == Decimal("0")


def _cycle(acct):
    return svc.due_cycles_in_horizon(acct, p_start=P_START, p_end=P_END)[0]


def test_target_full_balance_is_outstanding():
    acct = _FakeAccount(payment_strategy="full_balance")
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("1200.00")


def test_target_fixed_amount_is_literal():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("150.00"))
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("150.00")


@pytest.mark.parametrize("strategy", ["minimum_only", "custom_per_period"])
def test_target_stored_amount_when_present(strategy):
    acct = _FakeAccount(payment_strategy=strategy)
    cyc = _cycle(acct)  # close Apr 25 -> anchor (id, 2026, 4)
    per_cycle = {(acct.id, 2026, 4): Decimal("75.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("75.00")


@pytest.mark.parametrize("strategy", ["minimum_only", "custom_per_period"])
def test_target_zero_when_stored_amount_unset(strategy):
    acct = _FakeAccount(payment_strategy=strategy)
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("0")


def test_target_none_strategy_defaults_full_balance():
    acct = _FakeAccount(payment_strategy=None)
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("900.00"), {}) == Decimal("900.00")


def test_synth_full_balance_pays_total_owed_at_close():
    acct = _FakeAccount(payment_strategy="full_balance")
    ledger = [(date(2026, 4, 10), Decimal("-500.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("-400.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("900.00"))]


def test_synth_card_in_credit_no_outflow():
    acct = _FakeAccount(payment_strategy="full_balance")
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("50.00"), ledger=[], credits=[], per_cycle_amounts={},
    )
    assert pays == []


def test_synth_clamp_never_pays_into_credit():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("500.00"))
    ledger = [(date(2026, 4, 10), Decimal("-300.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("300.00"))]


def test_synth_real_payment_nets_p_k():
    acct = _FakeAccount(payment_strategy="full_balance")
    ledger = [(date(2026, 4, 10), Decimal("-1000.00"))]
    credits = [(99, date(2026, 4, 28), Decimal("300.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("0.00"), ledger=ledger, credits=credits, per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("700.00"))]


def test_synth_two_due_dates_s_prev_prevents_double_bill():
    acct = _FakeAccount(close_day=25, payment_strategy="full_balance")
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("1000.00"))]


def test_synth_consumed_credit_attributed_to_one_cycle():
    acct = _FakeAccount(close_day=25, payment_day=1, payment_day_relative_month=2,
                        payment_strategy="full_balance")
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]
    credits = [(7, date(2026, 5, 3), Decimal("200.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 6, 1), p_end=date(2026, 7, 31),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=credits, per_cycle_amounts={},
    )
    assert pays == [(date(2026, 6, 1), Decimal("800.00")), (date(2026, 7, 1), Decimal("200.00"))]
