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

# ISO 4217 active alphabetic codes. Excludes the historical/withdrawn set and
# the X-series test/no-currency codes (XXX, XTS) — neither is a currency a user
# should be able to denominate an account in.
ISO_4217_CURRENCIES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN
    BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD
    CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK
    DJF DKK DOP DZD
    EGP ERN ETB EUR
    FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD
    HKD HNL HRK HTG HUF
    IDR ILS INR IQD IRR ISK
    JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN
    NAD NGN NIO NOK NPR NZD
    OMR
    PAB PEN PGK PHP PKR PLN PYG
    QAR
    RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD USN UYI UYU UYW UZS
    VED VES VND VUV
    WST
    XAF XCD XCG XDR XOF XPF XSU XUA
    YER
    ZAR ZMW ZWG
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
    existing = (
        await db.execute(
            select(Account.currency)
            .where(Account.org_id == org_id)
            .limit(1)
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
