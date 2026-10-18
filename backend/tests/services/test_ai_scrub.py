"""TBD-628: ``ai_scrub`` removes IBANs, cards, emails and phones, and never an amount, date or id.

Pure (no DB). T1/T2 tables are the contract: moving a row between them is a deliberate edit.
"""
import json
import re
import time

import pytest

from app.services import ai_scrub
from app.services.ai_scrub import scrub_messages, scrub_text, scrub_value

IBAN, CARD, EMAIL, PHONE = "[IBAN]", "[CARD]", "[EMAIL]", "[PHONE]"

_M = {
    IBAN: """NL91ABNA0417164300|NL91 ABNA 0417 1643 00|nl91abna0417164300|DE89370400440532013000
        |DE89 3704 0044 0532 0130 00|GB29NWBK60161331926819|GB29 NWBK 6016 1331 9268 19
        |FR1420041010050500013M02606|FR14 2004 1010 0505 0001 3M02 606|BE68539007547034|BE68 5390 0754 7034
        |ES9121000418450200051332|IT60X0542811101000000123456|CH9300762011623852957|AT611904300234573201
        |PT50000201231234567890154|NO9386011117947|PL61109010140000071219812874|IE29AIBK93115212345678
        |LU280019400644750000|BR1800360305000010009795493C1""",
    CARD: """4111111111111111|4111 1111 1111 1111|4111-1111-1111-1111|4222222222222|5555555555554444
        |5555 5555 5555 4444|2223003122003222|378282246310005|3782 822463 10005|6011111111111117|6759649826438453""",
    EMAIL: "jan.jansen@example.nl|a@b.co|first.last+tag@sub.mail.example.co.uk|JAN_JANSEN@EXAMPLE.COM",
    PHONE: """+31612345678|+31 6 12345678|+31 6 1234 5678|+31 (0)6 12345678|0031 6 12345678|0031612345678
        |06-12345678|06 1234 5678|0612345678|020-1234567|020 123 4567|(020) 123 4567
        |+49 30 12345678|030 12345678|0151 23456789|+49 151 23456789
        |+44 20 7946 0958|+44 (0)20 7946 0958|020 7946 0958|07700 900123
        |+33 6 12 34 56 78|06 12 34 56 78|01 23 45 67 89|06-12-34-56-78
        |+32 470 12 34 56|0470 12 34 56|+34 612 34 56 78|+34 612 345 678|+39 06 1234 5678
        |+1 415 555 2671|+1-415-555-2671|(415) 555-2671|+1 (415) 555-2671""",
}
MATCH = [(r.strip(), tag) for tag, rows in _M.items() for r in rows.split("|")]
# Evasions (review): zero-width characters, full-width forms, no-break spaces, en dash.
MATCH += [
    ("NL91\u200bABNA0417164300", IBAN), ("06\u200b12345678", PHONE), ("jan\u200b@example.nl", EMAIL),
    ("４１１１ １１１１ １１１１ １１１１", CARD), ("NL９１ABNA0417164300", IBAN), ("jan＠example.nl", EMAIL),
    ("4111\xa01111\xa01111\xa01111", CARD), ("NL91\xa0ABNA\xa00417\xa01643\xa000", IBAN),
    ("+31\xa06\xa012345678", PHONE), ("06\u201312345678", PHONE),
]
CONTEXTS = [
    "{}", "Tel: {}", "IBAN: {}.", "call {} now", "/IBAN/{}/BIC/RABONL2U/", "x,{},y",
    json.dumps({"description": "{}"}, ensure_ascii=False),
]

NOT = [s.strip() for s in """1,234.56|1.234,56|-1234.56|1234567.89|€ 1.234.567,89|1 234 567,89|€1,234,567.89|$12,345,678.90
|0,50|0.000123456|0.0612345678|1234567890123.45|2345678901234.56|4111111111111111.00|-4111111111111111|12345678|1e+20
|+1 234 567|netto +1 250 000 EUR|Saldo: +1 234 567 kr|+1250000|+12500000|+125000000|+1 250 000 000
|100%|12.5%|-3.75%
|2026-10-15|15-10-2026|05-10-2026|05/10/2026|05.10.2026|20261015|01022026|15102026|Q3 2026
|2026-10-15T09:30:00Z|2026-10-15T09:30:00+02:00|2026-10-15 09:30:00|05-10-2026 09:30|05-10-2026 1430|05.10.2026 14:30
|01.02.26/12.34|09:30|14:30:00|+02:00|1760527200|1760527200000|1760527200123456|20261015093000|20261015093000123
|123|4242|1234567890|action_id 12345|550e8400-e29b-41d4-a716-446655440000|01234567-8901-2345-6789-012345678901
|3f2a9c0d1e4b5a6c7d8e9f0a1b2c3d4e|ref 0000000000|Factuur 0012345678|EREF 20261015123456789
|Betalingskenmerk 0123 4567 8901 2345|0123 4567 8901 2345|12 345 678 901 234|41111111111111110|0612345678901
|KvK 12345678|BTW NL001234567B01|postcode 1012 AB|NR:XXXXXX|,PAS123
|Ryanair FR1091 Dublin Amsterdam 120 EUR
|4111111111111112|4111 1111 1111 1112|4111 1111 1111 1112 25|NL91ABNA0417164301|NL91ABNA041716430|XX91ABNA0417164300
|NL91ABNA0417164300X|user@localhost|@handle|a@b|v1.2.3|10.0.0.1|192.168.1.163|1-800-FLOWERS|0 1 2 3|path/0612345678x
|415-555-2671|415.555.2671|01.23.45.67.89|06 - 12345678|4111  1111  1111  1111|4111.1111.1111.1111|jürgen@münchen.de
|sort code 12-34-56 account 12345678|2026 0612345678|0612345678 0201234567|**** **** **** 1234|+7 912 345 678""".replace("\n|", "|").replace("\n", "|").split("|")]
NOT_CONTEXTS = ["{}", "x {} y", '{"amount": "{}"}']


# One node per table, not per row x context: ~1000 parametrized ids would push
# backend/.test_durations coverage under its 80% floor until a runner regenerates it.
def test_t1_must_match():
    bad = [(row, ctx) for row, tag in MATCH for ctx in CONTEXTS
           if scrub_text(ctx.replace("{}", row)) != ctx.replace("{}", tag)]
    assert not bad, bad


def test_t2_must_not_match():
    bad = [(row, ctx) for row in NOT for ctx in NOT_CONTEXTS
           if scrub_text(ctx.replace("{}", row)) != ctx.replace("{}", row)]
    assert not bad, bad


def test_t2_accepted_misses_of_the_review_pinned_exactly():
    # Evasions the filter deliberately does not chase; a later widening is a deliberate edit.
    for src, out in [
        ("jan&#64;example.nl", "jan&#64;example.nl"),
        ("NL91.ABNA.0417.1643.00", "NL91.ABNA.0417.1643.00"),
        ("NL91-ABNA-0417-1643-00", "NL91-ABNA-0417-1643-00"),
        ("NL91 ABN A041 7164 300", "NL91 ABN A041 7164 300"),
        ("card4111111111111111", "card4111111111111111"),
        ("٤١١١ ١١١١ ١١١١ ١١١١", "٤١١١ ١١١١ ١١١١ ١١١١"),  # Arabic-Indic: NFKC keeps them
        ("NL91 ABNA 0417164300", "NL91 ABNA [PHONE]"),
    ]:
        assert scrub_text(src) == out, src


def test_t1_accepted_false_positives():
    for src, out in [("Klantnummer 0123456789", "Klantnummer [PHONE]"), ("05-10-26 1430", "[PHONE]"),
                     ("+12 345 678", "[PHONE]")]:
        assert scrub_text(src) == out


def test_t3_checksums_and_lengths():
    # NL58ABNA041716430 is mod-97 valid but 17 long (NL is 18): only the length check keeps it.
    for s in ["4111111111111112", "4111 1111 1111 1112", "NL91ABNA0417164301", "NL91ABNA041716430",
              "XX91ABNA0417164300", "NL58ABNA041716430"]:
        assert scrub_text(s) == s


T4 = [
    ("card 4111 1111 1111 1111 102 euro", "card [CARD] 102 euro"),
    ("4111 1111 1111 1111 25 euro", "[CARD] 25 euro"),
    ("4111 1111 1111 1111 12.50", "[CARD] 12.50"),
    ("2026 4111 1111 1111 1111", "2026 [CARD]"),
    ("4111 1111 1111 1112 25", "4111 1111 1111 1112 25"),
    ("+31 6 1234 5678 125 euro", "[PHONE] 125 euro"),
    ("+33 6 12 34 56 78 250 EUR", "[PHONE] 250 EUR"),
    ("0031 6 12345678 150", "[PHONE] 150"),
    ("+31 6 1234 5678 25", "[PHONE] 25"),
    ("+44 20 7946 0958 12", "[PHONE] 12"),
    ("0612345678 25 euro", "[PHONE] 25 euro"),
    ("bel 0612345678 2 keer", "bel [PHONE] 2 keer"),
    ("020 123 4567 89", "[PHONE] 89"),
    ("+31 6 1234 5678 12.50", "+31 6 1234 5678 12.50"),
    ("+34 612 345 678", "[PHONE]"),
    ("+1 234 567", "+1 234 567"),
    ("netto +1 250 000 EUR", "netto +1 250 000 EUR"),
    ("0612345678, 0201234567", "[PHONE], [PHONE]"),
    ("0612345678/0201234567", "[PHONE]/[PHONE]"),
    ("NL91ABNA0417164300 2026", "[IBAN] 2026"),
    ("BE68 5390 0754 7034 2026", "[IBAN] 2026"),
    ("XX12 NL91 ABNA 0417 1643 00", "XX12 [IBAN]"),
]


def test_t4_numbers_next_to_identifiers_survive():
    bad = [(src, scrub_text(src), out) for src, out in T4 if scrub_text(src) != out]
    assert not bad, bad


def test_t5_abn_lines():
    src = ("/TRTP/SEPA OVERBOEKING/IBAN/NL91ABNA0417164300/BIC/ABNANL2A/NAME/J Jansen/REMI/"
           "terugbetaling jan.jansen@example.nl 06-12345678/EREF/NOTPROVIDED")
    assert scrub_text(src) == ("/TRTP/SEPA OVERBOEKING/IBAN/[IBAN]/BIC/ABNANL2A/NAME/J Jansen/REMI/"
                               "terugbetaling [EMAIL] [PHONE]/EREF/NOTPROVIDED")
    pos = "BEA, Betaalpas  ALBERT HEIJN 1234,PAS123  NR:12AB34  01.02.26/12.34  AMSTERDAM"
    assert scrub_text(pos) == pos


_ROWS = [r for r, _ in MATCH] + NOT
_STRIP = re.compile(r"\[(?:IBAN|CARD|EMAIL|PHONE)\]")


def _subseq(a: str, b: str) -> bool:
    it = iter(b)
    return all(c in it for c in a)


def test_t6_idempotent_on_separated_joins():
    for a in _ROWS:
        for b in _ROWS:
            for sep in (" ", "/", "\n", ": ", ", ", " - ", "; "):
                s = a + sep + b
                once = scrub_text(s)
                assert scrub_text(once) == once, s


def test_t6_second_pass_only_removes_more_on_glued_joins():
    for a in _ROWS:
        for b in _ROWS:
            for glue in ("", "-", ".", ","):
                s = a + glue + b
                once = scrub_text(s)
                assert _subseq(_STRIP.sub("", scrub_text(once)), _STRIP.sub("", once)), s


def test_t6_known_growth_case():
    s = "a" * 60 + ".0612345678.@b.co"
    p1 = scrub_text(s)
    assert p1 == "a" * 60 + ".[PHONE].@b.co"
    assert scrub_text(p1) == "a" * 60 + ".[PHONE][EMAIL]"


T7 = [
    "5 " * 20000, "3 " * 20000, "2 " * 20000, "4 " * 20000, "5555 " * 8000, "4111 " * 8000,
    "3782 822463 " * 3300, "AB12 " * 8000, "AB12 CDEF " * 4000, "NL91 " * 8000,
    "NL91ABNA0417164301 " * 2100, "4111 1111 1111 1112 " * 2000, "a" * 40000, "0 " * 20000,
    "a@" * 20000, "a." * 20000, "+1 " * 13333, "1-" * 20000, "1234567890" * 4000,
    "a.b" * 13333 + "@", "(0" * 20000, "a" * 39990 + "@x", "a@" + "a" * 40000,
]


def test_t7_time_budget():
    slow = {}
    for s in T7:
        t = time.perf_counter()
        scrub_text(s)
        # Catastrophic backtracking on 40k chars costs seconds; local max is ~45ms, CI saw 258ms.
        if (dt := time.perf_counter() - t) >= 1.0:
            slow[f"{s[:14]!r}x{len(s)}"] = round(dt, 3)
    assert not slow, slow


def test_t7_preflight_size_input():
    t = time.perf_counter()
    scrub_text("AB12 " * 13000)
    assert time.perf_counter() - t < 1.0


def test_t8_scrub_value():
    v = {"d": "pay NL91ABNA0417164300 or 0612345678", "n": 4111111111111111, "id": 12, "amt": "1234.56",
         "l": ["a@b.co", 3], "b": True}
    assert scrub_value(v) == {"d": "pay [IBAN] or [PHONE]", "n": "[CARD]", "id": 12, "amt": "1234.56",
                              "l": ["[EMAIL]", 3], "b": True}
    assert v["n"] == 4111111111111111


def test_t8_json_property():
    for s, _ in [(r, None) for r in _ROWS]:
        j = json.dumps(scrub_value({"d": s, "n": [1, 4111111111111111]}), ensure_ascii=False)
        assert scrub_text(j) == j, s
        json.loads(j)


def test_t8_scrub_messages():
    img = {"type": "image_url", "image_url": {"url": "x"}}
    tc = [{"id": "1", "function": {"arguments": '{"to": "a@b.co"}'}}]
    msgs = [
        {"role": "user", "content": "mail a@b.co"},
        {"role": "user", "content": [{"type": "text", "text": "iban NL91ABNA0417164300"}, img]},
        {"role": "assistant", "content": None, "tool_calls": tc},
        "not a dict",
    ]
    snap = json.dumps(msgs)
    out = scrub_messages(msgs)
    assert out is not msgs
    assert out[0] == {"role": "user", "content": "mail [EMAIL]"}
    assert out[1]["content"] == [{"type": "text", "text": "iban [IBAN]"}, img]
    assert out[2] == msgs[2] and out[2]["tool_calls"] == tc
    assert out[3] == "not a dict"
    assert json.dumps(msgs) == snap


def test_module_is_stdlib_only():
    src = open(ai_scrub.__file__).read()
    assert set(re.findall(r"^(?:import|from) (\w+)", src, re.M)) <= {"re", "typing", "unicodedata"}
