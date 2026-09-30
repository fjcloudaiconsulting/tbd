"""Plan-write canonicalization. The single chokepoint for plans.features writes.

All plan create / update / duplicate paths run their incoming partial
features through canonicalize_features so storage stays canonical
(full closed-set, alias keys, strict bool).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.auth.feature_catalog import (
    ALL_FEATURE_KEYS,
    ALL_METER_KEYS,
    PlanFeatures,
    PlanUsageLimits,
)
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


def canonicalize_usage_limits(
    partial: Mapping[str, Mapping[str, Any]],
    existing: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge partial meter limits over the existing ones and return the
    canonical (all four meters, alias-keyed) dict.

    A meter in ``partial`` REPLACES that meter's ``{period, limit}`` wholesale
    (never a deep merge); the other meters keep their stored value. Unknown
    meters and every pydantic error (bad period, non-int or negative limit,
    null on a platform meter) raise ValidationError (HTTP 400 surface).
    """
    unknown = set(partial) - ALL_METER_KEYS
    if unknown:
        raise ValidationError(f"Unknown meters: {sorted(unknown)}")
    try:
        return PlanUsageLimits.model_validate({**(existing or {}), **partial}).model_dump(
            by_alias=True
        )
    except PydanticValidationError as exc:
        raise ValidationError(f"Invalid usage_limits: {exc.errors(include_url=False, include_input=False)}") from exc
