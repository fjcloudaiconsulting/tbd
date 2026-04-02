from pydantic import BaseModel, Field


class OrgSettingUpdate(BaseModel):
    key: str
    value: str


class BillingCycleUpdate(BaseModel):
    billing_cycle_day: int = Field(ge=1, le=28)


class OrgSettingResponse(BaseModel):
    key: str
    value: str

    model_config = {"from_attributes": True}
