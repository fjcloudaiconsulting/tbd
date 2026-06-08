"""Pydantic schemas for the L4.8 role admin API.

Slug pattern: ``^[a-z][a-z0-9_]{2,63}$`` — lowercase letter, then 2 to
63 letters/digits/underscores. Total length 3 to 64 (matches the DB
column). Locked at the schema layer so an invalid slug 422s before
it ever reaches the service.
"""
from __future__ import annotations

import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


SLUG_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"


class RoleListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str]
    is_system_frozen: bool
    permission_count: int
    created_at: datetime.datetime
    updated_at: datetime.datetime


class RoleListResponse(BaseModel):
    # ``total`` lets the admin roles table reuse the shared <Pagination>
    # component (matching the orgs / subscriptions / audit envelope).
    # Roles are a small, fully-listed set today, so ``total`` always
    # equals ``len(items)``; the field exists so the FE never has to
    # special-case its absence if server-side paging lands later.
    items: list[RoleListItem]
    total: int


class RoleDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str]
    is_system_frozen: bool
    permissions: list[str]
    created_at: datetime.datetime
    updated_at: datetime.datetime


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN, min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    """Patch shape — every field optional, only provided fields apply.

    ``permissions`` semantic: when provided (non-None), replaces the
    full set. ``[]`` clears all. Omitting the key leaves them
    untouched.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    permissions: Optional[list[str]] = None


class PermissionCatalogEntry(BaseModel):
    key: str
    namespace: str


class PermissionCatalogResponse(BaseModel):
    namespaces: dict[str, list[str]]  # e.g. {"admin": ["admin.view"], ...}
    keys: list[str]
