from app.services.ai_feature_map import AI_FEATURE_MAP, ui_to_routing
from app.auth.feature_catalog import FeatureKey  # Literal of entitlement keys
from app.models.org_ai_routing import ROUTABLE_FEATURE_NAMES
import typing


def test_map_keys_align_with_catalog_and_routing():
    catalog_keys = set(typing.get_args(FeatureKey))
    for ent_key, routing_name, ui_id in AI_FEATURE_MAP:
        assert ent_key in catalog_keys, f"{ent_key} missing from feature catalog"
        assert routing_name in ROUTABLE_FEATURE_NAMES, f"{routing_name} not routable"


def test_ui_to_routing_resolves_and_rejects_unknown():
    assert ui_to_routing("forecast") == "smart_forecast"
    import pytest
    with pytest.raises(KeyError):
        ui_to_routing("nope")
