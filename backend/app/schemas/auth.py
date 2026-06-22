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
    # Cloudflare Turnstile tokens are bounded at 2048 chars; the bound
    # also prevents an attacker from pinning the server in the siteverify
    # POST with an unbounded payload. The field is treated as sensitive
    # by ``main.py`` 422 redaction (`_SENSITIVE_FIELD_NAMES`).
    captcha_token: str | None = Field(default=None, max_length=2048)


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(max_length=128)


class ResendVerificationPublicRequest(BaseModel):
    login: str = Field(min_length=1, max_length=120)


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
    # Founding-members program. True for users in the founder window
    # (default True so existing tokens/responses stay valid).
    is_founder: bool = True
    mfa_enabled: bool = False
    # Track E: org-level toggle for the "set balance directly" admin escape
    # hatch. Off by default; mirrors `billing_cycle_day` storage pattern
    # (column on `organizations`).
    allow_manual_balance_adjustment: bool = False
    # False for SSO-created users who have not yet set a password. The
    # frontend uses this to flip the Change Password card into "Set a
    # Password" mode and to show the "Verify with Google" button on the
    # email-change form.
    password_set: bool = True
    # L3.3: ISO-8601 timestamp when the user finished the first-run
    # wizard, or None if they have not yet seen it. Server-side flag
    # so it survives device changes; the frontend redirects to
    # ``/onboarding`` while this is None.
    onboarded_at: str | None = None
    subscription_status: str | None = None
    subscription_plan: str | None = None
    trial_end: str | None = None

    model_config = {"from_attributes": True}


class VerifyResponse(BaseModel):
    user: UserResponse
    access_token: str
    token_type: str = "bearer"


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


class StepUpInitiateRequest(BaseModel):
    # Short tag chosen by the frontend ("settings" for email change,
    # "security" for first-time password set). Validated against the
    # allowlist in routers/auth.py; anything else falls back to the
    # default settings page rather than 4xx so an old client never
    # breaks.
    return_to: str | None = Field(default=None, max_length=32)
