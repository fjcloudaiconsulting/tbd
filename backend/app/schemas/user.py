from pydantic import BaseModel, EmailStr, Field, field_validator


class ProfileUpdate(BaseModel):
    # Base bounds only (DoS protection). The 3-char minimum and pattern
    # enforced at /register are applied in the PUT /users/me handler
    # *only when the value actually changes* — legacy users with a
    # grandfathered 1- or 2-char name must still be able to update
    # their other profile fields (email, phone, name) without hitting
    # the strict rule.
    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: EmailStr | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    avatar_url: str | None = Field(default=None, max_length=500)

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError("Avatar URL must be an HTTP(S) URL")
        return v


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
