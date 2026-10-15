"""Remove IBANs, card numbers, email addresses and phone numbers from text sent to an AI (TBD-628).

``scrub_text`` first normalizes the text (``_normalize``: NFKC, zero-width characters dropped,
no-break spaces and tabs to a space, en dash to a hyphen; the returned text is the normalized
one), then replaces, in this order, IBANs, emails, cards and phones, each pass seeing the earlier
placeholders. IBANs and cards are only replaced when a checksum (mod-97 with the SWIFT length,
Luhn) agrees, and every pattern is biased to miss a borderline identifier rather than eat an
amount, date or id. Pure: stdlib only, no logging, no settings, raises nothing on any ``str``.
"""
import re
import unicodedata
from typing import Any

IBAN, CARD, EMAIL, PHONE = "[IBAN]", "[CARD]", "[EMAIL]", "[PHONE]"

# SWIFT IBAN registry: country -> total length.
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BI": 27, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22,
    "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32,
    "LI": 21, "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28, "NL": 18, "NO": 15, "OM": 23, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31,
    "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25, "SV": 28, "TL": 23,
    "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}

# Candidates; a checksum (mod-97, Luhn) decides. Every pattern starts only at a
# token start (lookbehind) so a failed attempt costs one bounded run, never O(n^2).
_IBAN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}"
    r"(?:[A-Za-z0-9]{11,30}|(?: [A-Za-z0-9]{4}){2,7}(?: [A-Za-z0-9]{1,3})?)(?![A-Za-z0-9])"
)
_CARD_RE = re.compile(
    r"(?<![\w+-])(?<!\d[.,])(?:[2-6]\d{12,18}"            # unbroken
    r"|[2-6]\d{3}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,3})?"        # 4-4-4(-4), optional 1-3 digit tail
    r"|3\d{3}[ -]\d{6}[ -]\d{4,5})"                         # Amex 4-6-5, Diners 4-6-4
    r"(?!\w|[.,-]\d)"
)
_EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[\w.%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}(?![\w-])"
)
_PHONE_RE = re.compile(
    r"(?<![\w+-])(?<!\d[.,])(?:"
    r"\+[1-9]\d{0,2}(?:[ -]?\(0\))?(?:[ -]?\d){6,12}"                   # +31 6 1234 5678, +44 (0)20 7946 0958
    r"|(?:\+?1[ -]?)?\(\d{3}\)[ -]?\d{3}[ -]?\d{4}"                       # (415) 555-2671
    r"|\(0\d{1,4}\)[ -]?\d(?:[ -]?\d){4,8}"                               # (020) 123 4567
    r"|(?<!\d )(?:00[1-9]\d{0,2}(?:[ -]?\d){6,12}"                         # 0031 6 12345678
    r"|(?!(?:0[1-9]|[12]\d|3[01])[-/.](?:0[1-9]|1[0-2])[-/.](?:19|20)\d\d)"  # not dd-mm-yyyy
    r"0[ -]?[1-9](?:[ -]?\d){7,10})"                                         # 06-12345678, 020 123 4567
    r")(?!\w|[ .,-]?\d)"
)


def _mod97(s: str) -> bool:
    s = s.replace(" ", "").upper()
    return IBAN_LENGTHS.get(s[:2]) == len(s) and int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def _luhn(s: str) -> bool:
    d = [int(c) for c in s if c.isdigit()]
    return 13 <= len(d) <= 19 and sum(x if i % 2 == 0 else (x * 2 - 9 if x > 4 else x * 2)
                                      for i, x in enumerate(reversed(d))) % 10 == 0


def _short_tail(s: str) -> bool:
    # A 1-2 digit last group after a longer one is a count or an amount ("06 12345678 25"), not part of the number.
    g = re.findall(r"\d+", s)
    return len(g) > 1 and len(g[-1]) <= 2 and len(g[-2]) > 2


def _card(s: str) -> bool:
    # Luhn first (cheap, fails 9 in 10). A "2" card is Mastercard 2221-2720 only: keeps years out.
    return _luhn(s) and (s[0] != "2" or "2221" <= s[:4] <= "2720") and not _short_tail(s)


def _phone(m: re.Match) -> str:
    s = m.group()
    g = re.findall(r"\d+", s.replace("(0)", " "))
    n, plus = sum(map(len, g)), s[0] == "+"
    if plus and len(g) == 1 and n < 10:            # +1250000: a signed amount
        return s
    if s.startswith("00") and n < 11:              # 0012345678: a reference
        return s
    if plus and len(g) > 1 and len(g[0]) == 1 and all(len(x) == 3 for x in g[1:]):   # +1 234 567: thousands
        return s
    # Leave trailing groups out while the rest is already a whole number:
    # 9+ digits after the country code, or 10+ digits nationally ("+31 6 1234 5678 125").
    head, need = (len(g[0]), 9) if plus or s.startswith("00") else (0, 10)
    tail = ""
    while len(g) > 1 and n - len(g[-1]) - head >= need:
        cut = max(s.rfind(" "), s.rfind("-"))
        s, tail, n = s[:cut], s[cut:] + tail, n - len(g.pop())
    return PHONE + tail


def _checked(rx: re.Pattern, ok, tag: str, min_len: int, text: str) -> str:
    """Replace each candidate's SHORTEST prefix ending at a space (or the whole candidate)
    with at least ``min_len`` alphanumerics that passes ``ok``. On none, resume at the
    candidate's next group (an IBAN or card can start there), or after it when unbroken.
    ``rx.search(text, pos)`` keeps lookbehinds on the full text."""
    out, pos = [], 0
    while (m := rx.search(text, pos)) is not None:
        s, cand = m.start(), m.group()
        cut = 0
        for k in [i for i, ch in enumerate(cand) if ch == " "] + [len(cand)]:
            if k - cand.count(" ", 0, k) - cand.count("-", 0, k) >= min_len and ok(cand[:k]):
                cut = k
                break
        if not cut:
            cut = min((i for i in (cand.find(" "), cand.find("-")) if i > 0), default=len(cand) - 1) + 1
            out.append(text[pos:s + cut])
        else:
            out.append(text[pos:s] + tag)
        pos = s + cut
    out.append(text[pos:])
    return "".join(out)


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
_SPACES = {ord(c): " " for c in "\u00a0\u202f\u2007\t"}


def _normalize(text: str) -> str:
    """One step before matching, so full-width digits, zero-width characters and
    no-break spaces cannot hide an identifier. NFKC also maps full-width ``@``."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return text.translate(_SPACES).replace("\u2013", "-")


def scrub_text(text: str) -> str:
    """``text`` normalized, with every IBAN, card number, email address and phone number replaced
    by its placeholder. Pure, no logging; order matters (each pass sees the earlier ones' placeholders)."""
    text = _checked(_IBAN_RE, _mod97, IBAN, 15, _normalize(text))
    text = _EMAIL_RE.sub(EMAIL, text)
    text = _checked(_CARD_RE, _card, CARD, 13, text)
    return _PHONE_RE.sub(_phone, text)


def scrub_value(value: Any) -> Any:
    """``scrub_text`` on every string in a JSON-able value (a new value; keys untouched)."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(v) for v in value]
    if type(value) is int and 10**12 <= value < 10**19 and scrub_text(str(value)) == CARD:
        return CARD  # a bare card number as a JSON int: same verdict as in text
    return value


def scrub_messages(messages: list[dict]) -> list[dict]:
    """A new list: each message's ``content`` scrubbed (a str, or the ``text`` of
    each multimodal part). Roles, ``tool_calls`` and the caller's list are untouched."""
    def part(p: Any) -> Any:
        return {**p, "text": scrub_text(p["text"])} if isinstance(p, dict) and isinstance(p.get("text"), str) else p

    out = []
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            m = {**m, "content": scrub_text(c)}
        elif isinstance(c, list):
            m = {**m, "content": [part(p) for p in c]}
        out.append(m)
    return out
