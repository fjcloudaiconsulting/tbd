"""Plan-write canonicalization. The single chokepoint for plans.features writes.

All plan create / update / duplicate paths run their incoming partial
features through canonicalize_features so storage stays canonical
(full closed-set, alias keys, strict bool).
"""
from __future__ import annotations

from collections.abc import Mapping

from app.auth.feature_catalog import ALL_FEATURE_KEYS, PlanFeatures
from app.services.exceptions import ValidationError


def canonicalize_features(
    partial: Mapping[str, bool],
    existing: Mapping[str, bool] | None = None,
) -> dict[str, bool]:
    """Merge a partial feature dict with the existing one and return
    the canonical (full closed-set, alias-keyed) dict.

    Raises ValidationError on unknown feature keys (HTTP 400 surface).
    """
    unknown = set(partial) - ALL_FEATURE_KEYS
    if unknown:
        raise ValidationError(f"Unknown feature keys: {sorted(unknown)}")

    merged = {**(existing or {}), **partial}
    return PlanFeatures.model_validate(merged).model_dump(by_alias=True)
