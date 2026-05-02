"""L4.11 — FastAPI dependencies for feature gating.

get_current_org_features is scoped to the authenticated user's own org;
DO NOT reuse it for cross-org admin reads. Admin endpoints call
feature_service.get_features(db, target_org_id) directly after
require_permission("orgs.view").
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.feature_catalog import ALL_FEATURE_KEYS, FeatureKey
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.services import feature_service
from app.services.feature_service import UnknownFeatureKey


_CACHE_ATTR = "_org_features_cache"


async def get_current_org_features(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Per-request cache of effective features for the auth user's own org.

    DO NOT use for cross-org admin reads — admin endpoints call
    feature_service.get_features(db, target_org_id) directly.
    """
    cache: dict[int, dict[str, bool]] = getattr(request.state, _CACHE_ATTR, None) or {}
    if user.org_id not in cache:
        cache[user.org_id] = await feature_service.get_features(db, user.org_id)
        setattr(request.state, _CACHE_ATTR, cache)
    return cache[user.org_id]


def require_feature(key: FeatureKey) -> Callable:
    """Dependency factory. Raises UnknownFeatureKey at module-import time
    if `key` isn't in the catalog — typos fail on backend startup, not
    at first request.
    """
    if key not in ALL_FEATURE_KEYS:
        raise UnknownFeatureKey(key)

    async def _dep(features=Depends(get_current_org_features)) -> dict[str, bool]:
        if not features[key]:
            raise HTTPException(
                status_code=403,
                detail={"code": "feature_not_enabled", "feature_key": key},
            )
        return features

    return _dep
