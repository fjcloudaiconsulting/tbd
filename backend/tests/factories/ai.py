"""TBD-627: the "AI is on" seed. AI is on for an org when it has a current
consent row AND the ``ai.*`` entitlement; tests that really dispatch get both
from one call."""
from __future__ import annotations

from app.config import settings
from app.models.feature_override import OrgFeatureOverride
from app.models.org_ai_consent import OrgAIConsent


def ai_on_rows(org_id: int, *features: str) -> list:
    """An ``OrgAIConsent`` at the configured version (read at call time) plus
    one ``OrgFeatureOverride`` (value True) per ``ai.*`` key in ``features``."""
    return [
        OrgAIConsent(org_id=org_id, consent_version=settings.ai_native_current_consent_version),
        *(OrgFeatureOverride(org_id=org_id, feature_key=f, value=True) for f in features),
    ]


AI_ALL = ("ai.agent", "ai.autocategorize", "ai.budget", "ai.forecast", "ai.smart_plan")
