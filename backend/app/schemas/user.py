from pydantic import BaseModel, EmailStr, Field, field_validator


class ProfileUpdate(BaseModel):
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
