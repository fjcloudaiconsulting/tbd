from pydantic import BaseModel, StrictBool


class AIFeatureState(BaseModel):
    entitled: StrictBool
    configured: StrictBool


class AIStatusResponse(BaseModel):
    categorize: AIFeatureState
    forecast: AIFeatureState
    budget: AIFeatureState
