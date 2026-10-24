"""New email addresses must be plain ASCII (DB-01).

``users.email`` compares under an accent-insensitive collation, so an address
with an accented local part or an internationalised domain can match a
different, ASCII address. New addresses are refused unless they are ASCII: at
sign-up (password and Google), on an invitation and on an email change.
Lookups and stored rows are untouched, so existing accounts sign in as before.

The check runs on the address as ``EmailStr`` returns it. That form decodes an
``xn--`` domain to Unicode, so an IDN domain is refused in either spelling; a
full-width spelling of an ASCII address folds to ASCII and is accepted.
"""
from __future__ import annotations

import unicodedata

NON_ASCII_EMAIL_DETAIL = (
    "Email addresses can contain only ASCII characters: accented letters and "
    "international domain names are not supported"
)


def is_ascii_email(value: str) -> bool:
    """True for an ASCII address whose domain has no IDNA (``xn--``) label."""
    if not value.isascii():
        return False
    domain = value.rpartition("@")[2]
    return not any(label.lower().startswith("xn--") for label in domain.split("."))


def reject_non_ascii_email(value: str | None) -> str | None:
    """Pydantic ``mode="after"`` validator for an ``EmailStr`` field."""
    if value is not None and not is_ascii_email(value):
        raise ValueError(NON_ASCII_EMAIL_DETAIL)
    return value


def google_sign_in_allowed(address: str, stored: str | None) -> bool:
    """May a Google-verified ``address`` sign in to the account the email
    lookup found (``stored``; None when it found none and would create one)?

    ASCII on both sides (or a new ASCII account) is the unchanged case. A
    non-ASCII address on either side is allowed only when it is the very
    address stored on the account (case and Unicode normalisation aside), so
    an existing non-ASCII account keeps signing in while a look-alike that the
    collation matches does not, and no new non-ASCII account is created.
    """
    if is_ascii_email(address) and (stored is None or is_ascii_email(stored)):
        return True
    if stored is None:
        return False
    return _nfc_lower(stored) == _nfc_lower(address)


def _nfc_lower(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip().lower())
