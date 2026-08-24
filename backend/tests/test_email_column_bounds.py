"""Every request field that reaches a ``String(120)`` email column is bounded.

TBD-362 follow-up. ``EmailStr`` accepts addresses up to 254 characters; every
email column in this schema is ``String(120)``. A syntactically VALID address
between 121 and 254 characters therefore passed validation, reached the
INSERT, and raised an unhandled 500 — MySQL ``DataError 1406`` under
``STRICT_TRANS_TABLES``.

⚠⚠ **THE SHARDS STRUCTURALLY CANNOT SEE THIS CLASS.** SQLite does not enforce
``VARCHAR(n)``: it stores the over-long value happily and every test stays
green. This module therefore asserts the **schema bound**, which is
backend-independent, and never the driver error, which is not. Same blindness
family as the collation traps documented in ``routers/auth.py``.

⚠ The first fix for this bounded only ``POST /admin/users/{id}/email-change``
— a superadmin-only, interactive-session-gated, rate-limited route — and left
``POST /api/v1/auth/register`` unbounded. That route is **public and
unauthenticated**, so the half-fix left the larger blast radius open while its
own comment established the defect class. This module exists so the next
schema that writes one of these columns cannot repeat that.

⚠ Keyed on the SCHEMA, not on a grep of the models: a whole-file grep for
``String(120)`` would be satisfied by any of the dozens of unrelated columns
of that width.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.admin_users import AdminEmailChangeRequest
from app.schemas.auth import RegisterRequest
from app.schemas.invitation import InvitationCreateRequest
from app.schemas.user import ProfileUpdate

# The width of every email column reached from a request body:
# ``users.email``, ``users.pending_email``, ``invitations.email`` and
# ``invitations.open_email`` are all ``String(120)``.
EMAIL_COLUMN_WIDTH = 120

_AT_DOMAIN = "@example.com"


def _address(total_length: int) -> str:
    """A syntactically valid address of exactly ``total_length`` characters."""
    local = "x" * (total_length - len(_AT_DOMAIN))
    assert local, "requested length is shorter than the domain"
    return local + _AT_DOMAIN


# ``(label, model, field, other_required_kwargs)``. Each entry names a request
# body whose value lands in a ``String(120)`` column.
BOUNDED_EMAIL_FIELDS = (
    ("register (PUBLIC, unauthenticated)", RegisterRequest, "email",
     {"username": "abcde", "password": "S3cret-Pass!"}),
    ("invitation create", InvitationCreateRequest, "email", {"role": "member"}),
    ("profile update (writes pending_email)", ProfileUpdate, "email", {}),
    ("admin email-change", AdminEmailChangeRequest, "new_email",
     {"new_email_confirm": _address(EMAIL_COLUMN_WIDTH), "reason": "a fence"}),
    ("admin email-change confirm", AdminEmailChangeRequest, "new_email_confirm",
     {"new_email": _address(EMAIL_COLUMN_WIDTH), "reason": "a fence"}),
)


@pytest.mark.parametrize(
    "label,model,field,extra",
    BOUNDED_EMAIL_FIELDS,
    ids=[e[0] for e in BOUNDED_EMAIL_FIELDS],
)
def test_an_over_long_but_valid_address_is_refused(label, model, field, extra):
    """121 characters is a 422, not a 500 at commit time.

    Wrong implementation killed: dropping ``max_length`` from the field, which
    restores the unhandled 500 on MySQL while leaving every shard green.
    """
    too_long = _address(EMAIL_COLUMN_WIDTH + 1)
    assert len(too_long) == EMAIL_COLUMN_WIDTH + 1

    with pytest.raises(ValidationError):
        model(**{**extra, field: too_long})


@pytest.mark.parametrize(
    "label,model,field,extra",
    BOUNDED_EMAIL_FIELDS,
    ids=[e[0] for e in BOUNDED_EMAIL_FIELDS],
)
def test_an_address_that_exactly_fits_is_accepted(label, model, field, extra):
    """The boundary is inclusive.

    Without this leg the fence above is satisfied by ``max_length=1``, which
    would refuse every real address — the inverse defect. Asserting both sides
    of the boundary is what makes the number mean 120 rather than "small".
    """
    exact = _address(EMAIL_COLUMN_WIDTH)
    assert len(exact) == EMAIL_COLUMN_WIDTH

    model(**{**extra, field: exact})
