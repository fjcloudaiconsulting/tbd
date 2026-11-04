"""Checks for names that other people read (BE-06).

A user's first and last name become the inviter name in invitation emails
sent from the product's own domain, so they get the org-rename rule
(``schemas/orgs.py``: no control characters) plus two more: no bidi
override/isolate characters, which can reorder what a reader sees, and no
web address, which mail clients turn into a link. The org name at
registration gets the org-rename rule only, so registration and rename
accept the same names.
"""
from __future__ import annotations

import re
import unicodedata

# U+202A..U+202E (embeddings and overrides) and U+2066..U+2069 (isolates).
# Other format characters stay allowed: ZWJ / ZWNJ are part of ordinary
# spelling in several scripts.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

# A scheme, a ``www.`` prefix, or a dotted host such as ``example.com``. The
# last one also refuses ``St.John``; ``St. John`` and ``J. R. Smith`` are
# accepted. Unicode-aware (``[^\W_]`` is any letter or digit, ``[^\W\d_]``
# any letter) and run on the NFKC form, so full-width letters and dots, and
# IDN hosts, are caught too: mail clients and browsers fold those into a
# working link.
_WEB_ADDRESS = re.compile(r"(?i)://|\bwww\.|(?:[^\W_]|-)\.[^\W\d_]{2,}")
# NFKC folds U+FF0E / U+FE52 / U+2024 to ".", but leaves the ideographic full
# stop (U+3002, also what U+FF61 folds to), which IDNA (UTS #46) still treats
# as a label separator.
_IDEOGRAPHIC_FULL_STOP = "\u3002"


def reject_control_characters(value: str | None) -> str | None:
    """The org-rename rule: C0 controls and DEL are refused."""
    if value is not None and any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("Name cannot contain control characters")
    return value


def check_display_name(value: str | None) -> str | None:
    """Refuse control characters (C0, DEL, C1), bidi controls and web addresses."""
    if value is None:
        return value
    if any(unicodedata.category(c) == "Cc" or c in _BIDI_CONTROLS for c in value):
        raise ValueError("Name cannot contain control characters")
    # Checked on a folded copy; the user's original spelling is what is stored.
    folded = unicodedata.normalize("NFKC", value).replace(_IDEOGRAPHIC_FULL_STOP, ".")
    if _WEB_ADDRESS.search(folded):
        raise ValueError("Name cannot contain a web address")
    return value
