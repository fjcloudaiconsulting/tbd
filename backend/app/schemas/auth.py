from pydantic import BaseModel, EmailStr, Field

# Single source of truth for username rules. Re-used by `RegisterRequest`
# below and by the `/check-username` Query constraint so the two stay
# consistent — no more "available now, rejected on submit" UX drift.
USERNAME_PATTERN = r"^[a-zA-Z0-9._-]+$"
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64


class RegisterRequest(BaseModel):
    # Letters, digits, dot, underscore, hyphen. No whitespace or other
    # punctuation, no unicode, no null bytes. 3-64 chars. Existing
    # shorter/looser names stay — the check only runs on new signups.
    username: str = Field(
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
    )
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    org_name: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MfaChallengeResponse(BaseModel):
    mfa_required: bool = True
    mfa_token: str


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
    mfa_enabled: bool = False
    subscription_status: str | None = None
    subscription_plan: str | None = None
    trial_end: str | None = None

    model_config = {"from_attributes": True}


class UsernameCheckResponse(BaseModel):
    available: bool
    suggestion: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(max_length=1024)
    new_password: str = Field(min_length=8, max_length=128)


class VerifyEmailRequest(BaseModel):
    token: str


# ── MFA ─────────────────────────────────────────────────────────────────────


class MfaSetupResponse(BaseModel):
    qr_code: str  # base64 PNG
    secret: str  # for manual entry
    uri: str  # otpauth:// URI


class MfaEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class MfaEnableResponse(BaseModel):
    recovery_codes: list[str]


class MfaDisableRequest(BaseModel):
    password: str = Field(max_length=128)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(max_length=2048)
    code: str = Field(min_length=6, max_length=6)


class MfaRecoveryRequest(BaseModel):
    mfa_token: str = Field(max_length=2048)
    code: str = Field(min_length=1, max_length=20)


class MfaEmailCodeRequest(BaseModel):
    mfa_token: str = Field(max_length=2048)


class MfaEmailVerifyRequest(BaseModel):
    mfa_token: str = Field(max_length=2048)
    email_token: str = Field(max_length=2048)
    code: str = Field(min_length=6, max_length=6)


class MfaRegenerateRequest(BaseModel):
    password: str = Field(max_length=128)
