"""AI status service — per-feature {entitled, configured} for the authenticated org.

`configured` is only evaluated when entitled, so an un-entitled org costs
zero routing lookups.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_routing_service, feature_service, usage_service
from app.services.ai_feature_map import AI_FEATURE_MAP


async def get_ai_feature_status(
    db: AsyncSession, *, org_id: int
) -> dict[str, dict[str, bool]]:
    """Return per-feature {entitled, configured} keyed by UI id.

    Keys: "categorize", "forecast", "budget", "agent".
    """
    features = await feature_service.get_features(db, org_id)
    out: dict[str, dict[str, bool]] = {}
    for ent_key, routing_name, ui_id in AI_FEATURE_MAP:
        entitled = bool(features.get(ent_key, False))
        configured = False
        if entitled:
            routing = await ai_routing_service.get_routing_for_feature(
                db, org_id=org_id, feature_name=routing_name
            )
            configured = routing is not None
        out[ui_id] = {"entitled": entitled, "configured": configured}
    return out


async def get_ai_status(db: AsyncSession, *, org_id: int, is_admin: bool) -> dict:
    """``GET /ai/status``: the feature states plus this period's meter usage
    (platform spend meters for org admins only)."""
    return {
        **await get_ai_feature_status(db, org_id=org_id),
        "usage": await usage_service.current_usage(db, org_id, include_platform=is_admin),
    }
