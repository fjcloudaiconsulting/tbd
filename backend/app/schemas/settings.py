from pydantic import BaseModel


class OrgSettingUpdate(BaseModel):
    key: str
    value: str


class OrgSettingResponse(BaseModel):
    key: str
    value: str

    model_config = {"from_attributes": True}
