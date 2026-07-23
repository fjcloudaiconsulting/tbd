"""Pure-math unit tests for cc_forecast_service (Credit Card Model V1, Slice 3).

DB-free. Proves the locked forecast invariants at the arithmetic level:
carried-balance-as-of-close B_k, the unified clamp+net resolver, S_prev
and consumed-credit threading, and every strategy's target.
"""
from datetime import date
from decimal import Decimal

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


def test_target_override_wins_for_full_balance():
    acct = _FakeAccount(payment_strategy="full_balance")
    cyc = _cycle(acct)  # close Apr 25 -> anchor (id, 2026, 4)
    per_cycle = {(acct.id, 2026, 4): Decimal("75.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("75.00")


def test_target_override_wins_for_none_strategy():
    acct = _FakeAccount(payment_strategy=None)
    cyc = _cycle(acct)
    per_cycle = {(acct.id, 2026, 4): Decimal("40.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("900.00"), per_cycle) == Decimal("40.00")


def test_target_override_wins_over_fixed_amount():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("150.00"))
    cyc = _cycle(acct)
    per_cycle = {(acct.id, 2026, 4): Decimal("60.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("60.00")


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


def test_synth_two_due_dates_with_new_charges_bills_only_the_delta():
    """S_prev must NOT zero out a legitimate second payment when the second
    cycle accrues NEW charges. Cycle 1 owes 1000 (pays 1000, S_prev=1000);
    cycle 2's balance-at-close is 1400 (a 400 charge landed after cycle 1's
    close), so its outflow is 1400-1000 = 400, not 0 and not 1400."""
    acct = _FakeAccount(close_day=25, payment_strategy="full_balance")
    ledger = [
        (date(2026, 3, 10), Decimal("-1000.00")),  # owed at both closes
        (date(2026, 5, 10), Decimal("-400.00")),    # new charge after Apr-25 close
    ]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("1000.00")), (date(2026, 6, 1), Decimal("400.00"))]


def test_synth_consumed_credit_attributed_to_one_cycle():
    # relative_month=2: Apr-25 close -> due Jun 1 (window (Apr25, Jun1]);
    # May-25 close -> due Jul 1 (window (May25, Jul1]). The two windows OVERLAP
    # on (May25, Jun1]. A credit dated May 28 sits INSIDE that overlap, so it is
    # a candidate for BOTH cycles; the consumed-set must attribute it to the
    # earliest owning cycle only. Without that guard cycle 2 would re-net 200
    # (yielding outflow 0, dropped) and the assertion below would fail.
    acct = _FakeAccount(close_day=25, payment_day=1, payment_day_relative_month=2,
                        payment_strategy="full_balance")
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]
    credits = [(7, date(2026, 5, 28), Decimal("200.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 6, 1), p_end=date(2026, 7, 31),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=credits, per_cycle_amounts={},
    )
    assert pays == [(date(2026, 6, 1), Decimal("800.00")), (date(2026, 7, 1), Decimal("200.00"))]


def test_synth_skips_degenerate_cycle_payment_on_or_before_close():
    """Belt-and-suspenders: a cycle whose payment_date <= close date has an
    empty credit-attribution window, so its p_k_owned is always 0 and any
    real payment-in leg would be ignored, overstating the projected
    outflow. The create/PUT validation guard now forbids this config
    (payment_day_relative_month=0 with payment_day <= close_day), but a
    clamp collision (e.g. Feb close_day=28 + payment_day=30 both clamp to
    Feb 28) can still produce payment_date == close_date. The synthesizer
    skips such cycles rather than emit an overstated payment.

    Here close_day=25 + payment_day=5 + same-month yields payment BEFORE
    close for every cycle, so nothing is synthesized despite carried debt.
    """
    acct = _FakeAccount(
        close_day=25, payment_day=5, payment_day_relative_month=0,
        payment_strategy="full_balance",
    )
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]  # owed at every close
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == []
