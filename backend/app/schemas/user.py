from pydantic import BaseModel, EmailStr


class ProfileUpdate(BaseModel):
    username: str | None = None
    email: EmailStr | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
