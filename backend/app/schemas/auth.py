from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str = Field(min_length=8)
    first_name: str | None = None
    last_name: str | None = None
    org_name: str | None = None


class LoginRequest(BaseModel):
    login: str  # accepts username or email
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    email_verified: bool = False
    role: str
    org_id: int
    org_name: str
    billing_cycle_day: int = 1
    is_superadmin: bool
    is_active: bool

    model_config = {"from_attributes": True}


class UsernameCheckResponse(BaseModel):
    available: bool
    suggestion: str | None = None
