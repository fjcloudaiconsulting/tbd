"""BE-06: names that reach other people's inboxes are validated at
registration and profile update (``schemas/display_name.py``)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import RegisterRequest
from app.schemas.user import ProfileUpdate

_REGISTER = {"username": "alice", "email": "alice@example.com", "password": "pw-1234567"}

ACCEPTED_NAMES = [
    "Anne-Marie", "O'Neil", "St. John", "José", "J. R. R.", "J. R. Smith", "Dr. Ana",
    "محمد\u200cرضا", "\u8fc8\u514b\u5c14\u00b7\u6770\u514b\u900a", "\uff2a\uff4f\uff53\u00e9",
]
REFUSED_NAMES = [
    "evil.example",
    "Visit www.x",
    "https://x",
    # Look-alikes a mail client or browser folds into a working link.
    "evil\uff0ecom",  # full-width full stop
    "evil\u3002com",  # ideographic full stop
    "evil\uff61com",  # half-width ideographic full stop
    "\uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d",  # full-width letters and dot
    "\uff57\uff57\uff57\uff0ex",  # full-width www.
    "https\uff1a\uff0f\uff0fx",  # full-width colon and solidus
    "\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444",  # IDN host
    "Ann\x00Marie",
    "Ann\x1bMarie",
    "Ann\x85Marie",
    "Ann\u202eMarie",
]


@pytest.mark.parametrize("name", ACCEPTED_NAMES)
def test_ordinary_names_are_accepted(name):
    assert RegisterRequest(**_REGISTER, first_name=name, last_name=name).first_name == name
    assert ProfileUpdate(first_name=name, last_name=name).last_name == name


@pytest.mark.parametrize("name", REFUSED_NAMES)
@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_register_refuses_unsafe_names(field, name):
    with pytest.raises(ValidationError):
        RegisterRequest(**_REGISTER, **{field: name})


@pytest.mark.parametrize("name", REFUSED_NAMES)
@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_profile_update_refuses_unsafe_names(field, name):
    with pytest.raises(ValidationError):
        ProfileUpdate(**{field: name})


def test_original_spelling_is_stored_not_the_folded_form():
    name = "\uff2a\uff4f\uff53\u00e9"  # full-width letters, accepted
    assert ProfileUpdate(first_name=name).first_name == name


def test_omitted_and_null_names_still_pass():
    assert ProfileUpdate().first_name is None
    assert ProfileUpdate(first_name=None, last_name=None).last_name is None


def test_register_org_name_follows_the_org_rename_rule():
    """Control characters refused, as on rename; nothing stricter."""
    with pytest.raises(ValidationError):
        RegisterRequest(**_REGISTER, org_name="Acme\x07")
    assert RegisterRequest(**_REGISTER, org_name="Acme.io Household").org_name == "Acme.io Household"
