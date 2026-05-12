"""User-creation and identity-related helpers.

Centralizes the email normalization rule used at every site that
either inserts a ``users`` row or looks one up by email. The rule:

    normalize_email(email) := email.strip().lower()

Both layers (Python + DB collation) enforce the same shape so we
catch any drift on either side:

- Python normalizes before insert and before lookup. This guarantees
  the value stored in ``users.email`` is already lowercased and
  trimmed regardless of what an upstream caller (or Google's
  userinfo payload) supplies.
- The MySQL column inherits the default ``utf8mb4_0900_ai_ci``
  collation. Belt-and-suspenders: even if a future migration changes
  the collation, the Python pre-pass keeps duplicates from sneaking
  past the unique constraint via casing.

Pre-launch we have a real user with duplicate rows (one local, one
SSO at the same address) caused by an earlier version of the
Google callback that didn't dedupe by email. The
``POST /api/v1/admin/users/merge`` endpoint is the recovery path
for those rows; see ``app/services/user_merge_service.py``.
"""
from __future__ import annotations


def normalize_email(value: str) -> str:
    """Canonical form for storage and lookup. Idempotent.

    No `email.utils.parseaddr` parsing — incoming values are
    already validated by Pydantic's ``EmailStr``, so the only
    normalization we layer on is whitespace trim + lowercase.
    """
    return value.strip().lower()
