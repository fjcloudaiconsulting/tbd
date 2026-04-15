from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "PFV2"
    app_env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "mysql+aiomysql://pfv2:pfv2_secret@mysql:3306/pfv2"

    # Auth
    jwt_secret_key: str = "change-me-generate-a-real-secret"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "HS256"
    session_lifetime_days: int = 30  # absolute max session duration

    # Cookies — True in production (HTTPS), False in dev (HTTP)
    cookie_secure: bool = True

    # Redis (optional — used for sessions/cache in production)
    redis_url: str = ""

    # Email (Mailgun)
    mailgun_api_key: str = ""
    mailgun_domain: str = ""
    mailgun_region: str = ""  # "eu" for EU endpoint, empty for US
    email_from: str = "PFV2 <noreply@pfv.app>"
    app_url: str = "http://localhost"  # used for email links

    # MFA
    mfa_encryption_key: str = ""  # Fernet key for encrypting TOTP secrets

    # Google SSO
    google_client_id: str = ""
    google_client_secret: str = ""

    # CORS
    backend_cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",")]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
