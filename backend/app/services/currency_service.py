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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.user import Organization
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
    # ⚠ TBD-508 MITIGATION: LOCK THE ORG ROW FIRST. This is a lock-ORDER fix,
    # not decoration.
    #
    # The ``FOR UPDATE`` below takes a GAP lock when the org has no accounts
    # (an empty range), and a gap lock conflicts with the other transaction's
    # insert-intention lock. Two concurrent ``POST /accounts`` on a fresh org
    # therefore deadlock: MySQL error 1213, and the loser gets a 500. Measured
    # 2026-09-12 on MySQL 8.4.11 -- 3 of 12 concurrent trials before this PR,
    # and 6 of 12 with PR 2's ``UPDATE organizations`` added to the same
    # transaction, because that second exclusive lock widens the window.
    #
    # Taking the ``organizations`` row lock FIRST gives every writer on this
    # path the same order (organizations -> accounts), which is what removes
    # the inversion. It also happens to be the row this function goes on to
    # UPDATE, so it is a lock we need regardless.
    #
    # ⚠ AIOSQLITE CANNOT SEE ANY OF THIS. It ignores ``with_for_update()``,
    # has no gap locks and no error 1213, and every backend shard runs on it --
    # which is exactly why the deadlock shipped in PR 1 unnoticed. The suite
    # passing is not evidence this is fixed; the measurement is. Full fix
    # (retry vs. re-ordering, and where a fence for it can even live) is
    # TBD-508.
    await db.execute(
        select(Organization.id)
        .where(Organization.id == org_id)
        .with_for_update()
    )

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
        #
        # TBD-325 PR 2: THE ONLY WRITER of ``organizations.primary_currency``.
        # It lands here, inside the gap lock taken above, because that lock is
        # what makes "this is the first account" true for the duration of the
        # write. A second writer (a settings endpoint, an admin field) would
        # be a second source of truth for a fact ``accounts.currency`` already
        # holds -- the exact defect this ticket removes.
        await db.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(primary_currency=normalise_currency(currency))
        )
        return

    if normalise_currency(existing) != normalise_currency(currency):
        raise ConflictError(
            f"This organization's accounts are in {normalise_currency(existing)}. "
            f"An organization cannot hold accounts in more than one currency, "
            f"because totals across currencies would be meaningless. "
            f"Create the account in {normalise_currency(existing)}."
        )


async def resolve_currency_scope(db: AsyncSession, *, org_id: int) -> dict:
    """The in-band scope declaration for every scoped aggregate response.

    ``{currency, excluded_currencies, excluded_account_count}`` -- THREE keys.

    ONE query (a LEFT JOIN of the org row onto its accounts, grouped), resolved
    ONCE per call and threaded through, so a response can never carry two
    different answers and the cost is one statement, not two.

    IN-BAND rather than client-derived because the deciding consumer is
    ``ai_forecast_refine_service``, which consumes ``compute_forecast``
    service-to-service with the live session: no HTTP response, no React tree,
    so a client-derived banner cannot reach it by construction.

    ⚠ These three keys ARE the wire contract (``schemas/forecast.CurrencyScope``)
    and nothing else may join them. A fourth, internal-only key was tried and
    ``test_response_model_validates_and_preserves_wire_shape`` caught it on the
    response-model round-trip -- correctly. The predicate takes the whole dict
    and decides for itself; see ``transaction_filters.org_currency_filter``.
    """
    rows = (
        await db.execute(
            select(Organization.primary_currency, Account.currency,
                   func.count(Account.id))
            .select_from(Organization)
            .outerjoin(Account, Account.org_id == Organization.id)
            .where(Organization.id == org_id)
            .group_by(Organization.primary_currency, Account.currency)
        )
    ).all()
    # ⚠ NORMALISE BOTH SIDES. An earlier cut normalised only ``code`` and
    # compared against a RAW ``primary_currency``, which is a live defect the
    # moment a legacy row is lowercase: migration 081 backfills
    # ``MIN(a.currency)`` off a column that was FREE TEXT before PR 1, so an
    # org holding a single ``'eur'`` account gets ``primary_currency = 'eur'``,
    # and ``normalise_currency('eur') != 'eur'`` is TRUE. That org then reports
    # ITSELF as excluded: the tile drops its verdict and prints "Covers your
    # eur accounts only. 1 account in eur is not included", the Sankey emits a
    # multi-currency warning for one currency, and every aggregate grows the
    # subquery the short-circuit exists to avoid. Measured: 6 of 7 statements.
    #
    # ⚠ It is UNRECOVERABLE through the product: ``accounts.currency`` is
    # immutable post-create and ``primary_currency``'s single writer only fires
    # on a zero-account org. Only direct SQL repairs it.
    #
    # The migration now uppercases too, so this is defence in depth rather than
    # the sole guard -- deliberately, because "production is all EUR today" is
    # a DATA property being used to excuse a CODE property, which is the exact
    # shape this repo has been burned by before.
    org_currency = (
        normalise_currency(rows[0][0]) if rows and rows[0][0] else None
    )
    excluded = [
        (code, n)
        for _, code, n in rows
        if code is not None
        and org_currency is not None
        and normalise_currency(code) != org_currency
    ]
    count = sum(n for _, n in excluded)
    return {
        "currency": org_currency,
        "excluded_currencies": sorted(code for code, _ in excluded),
        "excluded_account_count": count,
    }


def currency_warning(currency_scope: dict) -> str | None:
    """Non-blocking notice that money was left out of an aggregate (TBD-325 PR 2).

    ⚠ ONE string, THREE consumers (sankey, reports, and any future scoped
    surface). It lives here rather than beside any one of them because
    ``sankey_service`` already imports from ``reports_query_service``, so a
    copy in either would be a circular import -- and two copies of a
    user-facing sentence drift.

    ``None`` when nothing was excluded, which is the 99% path -- the same
    short-circuit ``org_currency_filter`` makes, read off the same key so the
    two can never disagree. Voice matches ``reports/sources/networth.py``'s
    multi-currency warning: state what is shown, then why, in one sentence.
    """
    count = currency_scope["excluded_account_count"]
    if not count:
        return None
    accounts = "account" if count == 1 else "accounts"
    others = ", ".join(currency_scope["excluded_currencies"])
    return (
        f"Multiple currencies held; showing {currency_scope['currency']} only "
        f"({count} {accounts} in {others} excluded, because currencies are "
        "never summed)."
    )
