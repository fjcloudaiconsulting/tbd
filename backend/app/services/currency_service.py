"""Currency validation and the one-currency-per-org door (TBD-325, PR 1).

Why this exists
---------------
``transactions`` has no currency column — currency lives only on
``accounts.currency`` (``models/account.py``) — and none of the period
aggregates join ``Account``. So an org holding accounts in two currencies gets
budget bars, On Track, the spending donut and the forecast projection all
reporting ``EUR + USD`` as a single unlabelled number.

A production measurement on 2026-09-03 found **zero** multi-currency orgs (52
accounts, 19 orgs, all EUR, every value length 3). The defect is entirely
prospective, which is why the cheap prevention lands first: close the door
while the affected cohort is still empty, then do the ``primary_currency``
migration and the six-site aggregate scoping (TBD-325 PR 2, TBD-304).

⚠ This module does NOT make the aggregates currency-correct. It only stops an
org reaching the state in which they are wrong.

Why a hardcoded table rather than a library
-------------------------------------------
The repo has no currency dependency and adding one for a membership test is
not worth the supply-chain surface. ISO-4217's active list changes rarely (a
handful of codes a decade) and a stale entry here fails CLOSED — a user is
refused a currency that exists, which is visible and reportable. The inverse,
accepting a code that is not a currency, is the failure this table prevents.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.services.exceptions import ConflictError

# ISO 4217 codes a consumer bank account can actually be denominated in.
#
# ⚠ THIS LIST IS DELIBERATELY NARROWER THAN "ISO 4217 ACTIVE". Three exclusions,
# all on the same principle — a user must not be able to pick something that is
# not money they can hold in an account:
#
#   1. Test / no-currency codes: XXX, XTS.
#   2. Fund and unit-of-account codes: BOV, CHE, CHW, CLF, COU, MXV, USN, UYI,
#      UYW, XSU, XUA, and XDR (the IMF basket). These are index units and
#      settlement baskets — Mvdol, WIR, Chilean UF, Colombian UVR, Mexican UDI,
#      next-day funds, Uruguayan indexed units, SUCRE, the ADB unit. None
#      appears on a bank statement and none has a usable symbol.
#   3. Withdrawn currencies: HRK (Croatian kuna, withdrawn 2023-01-01 when
#      Croatia adopted the euro) and ANG (Netherlands Antillean guilder,
#      withdrawn 2025-03-31, replaced by XCG which IS present).
#
# ⚠⚠ WHY A WRONG ENTRY HERE IS NOT SYMMETRIC WITH A MISSING ONE.
# A MISSING currency fails closed: a user is refused something real, which is
# visible and reportable. A currency that should NOT be here fails OPEN, and
# because ``AccountUpdate`` has no currency field the choice is IMMUTABLE — an
# org that picks a withdrawn or non-monetary code is permanently locked to it
# with no edit path and no admin repair. Adding to this list is therefore the
# dangerous direction, not removing from it.
#
# ⚠ Removing a code is a one-way door for any org already holding it: the
# schema validator would reject that org's own currency with 422 BEFORE the
# single-currency check runs, so it could never create another account. Before
# dropping anything, query production for orgs holding it.
ISO_4217_CURRENCIES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM
    BBD BDT BGN BHD BIF BMD BND BOB BRL BSD
    BTN BWP BYN BZD CAD CDF CHF CLP CNY COP
    CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN
    ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
    GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD
    IRR ISK JMD JOD JPY KES KGS KHR KMF KPW
    KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR
    MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD
    OMR PAB PEN PGK PHP PKR PLN PYG QAR RON
    RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP
    SLE SOS SRD SSP STN SVC SYP SZL THB TJS
    TMT TND TOP TRY TTD TWD TZS UAH UGX USD
    UYU UZS VED VES VND VUV WST XAF XCD XCG
    XOF XPF YER ZAR ZMW ZWG
    """.split()
)


def normalise_currency(raw: str) -> str:
    """Uppercase and strip, so validation runs on the canonical form.

    ⚠ ORDER MATTERS. Validating before normalising refuses ``" eur "`` — a
    realistic paste — and refuses the conventional lowercase form that any
    direct API caller would send. The browser happens to uppercase before
    submitting (``accounts/page.tsx``), which is exactly what would make a
    validate-first bug invisible in the UI and visible only to API clients.
    """
    return (raw or "").strip().upper()


def is_supported_currency(code: str) -> bool:
    return normalise_currency(code) in ISO_4217_CURRENCIES


async def assert_org_currency_allows(
    db: AsyncSession, *, org_id: int, currency: str
) -> None:
    """Refuse a currency that disagrees with the one the org already holds.

    ⚠ ISO validation alone is NOT sufficient, and that is the point of this
    function: EUR and USD are both real currencies, so a code-only check closes
    the typo door and leaves the deliberate one wide open — and the deliberate
    one produces exactly the same wrong aggregates.

    Scoped per-org. A global check would make the first org's choice the
    product's currency.

    Called BEFORE any insert, so a refusal writes no row. A handler that
    inserted first and validated second would still return 409 while leaving
    the org in the multi-currency state this exists to prevent.
    """
    # ⚠ LOCKING READ, not a plain SELECT. Under MySQL InnoDB's REPEATABLE READ a
    # plain SELECT is a non-locking consistent read, so two simultaneous
    # POST /accounts into a FRESH org (say EUR and USD) would both see zero rows
    # and both insert — landing the org in exactly the multi-currency state this
    # function exists to prevent, reachable on day one.
    #
    # ``with_for_update()`` on an empty result still takes an InnoDB gap lock on
    # the org_id range, which serialises the second transaction behind the first.
    # The second then sees the committed row and refuses.
    #
    # ⚠ THE TESTS CANNOT PROVE THIS. They run on aiosqlite, which ignores
    # ``with_for_update()`` entirely (same caveat recorded in
    # tests/routers/test_accounts_change_type.py). The lock is justified by the
    # engine's documented semantics, not by a green fence — do not read the
    # suite passing as evidence the race is closed, and do not remove this
    # because "no test covers it".
    #
    # A UNIQUE constraint cannot express this rule: it is "at most one DISTINCT
    # currency per org", not "one row per org". A generated column or a trigger
    # could, and would be a stronger guarantee if this ever needs one.
    existing = (
        await db.execute(
            select(Account.currency)
            .where(Account.org_id == org_id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if existing is None:
        # First account in the org: nothing to disagree with. Every supported
        # currency must remain reachable here, or this ships a EUR-only product.
        return

    if normalise_currency(existing) != normalise_currency(currency):
        raise ConflictError(
            f"This organization's accounts are in {normalise_currency(existing)}. "
            f"An organization cannot hold accounts in more than one currency, "
            f"because totals across currencies would be meaningless. "
            f"Create the account in {normalise_currency(existing)}."
        )
