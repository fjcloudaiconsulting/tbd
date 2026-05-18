from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "The Better Decision"
    app_env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "mysql+aiomysql://pfv2:pfv2_secret@mysql:3306/pfv2"

    # SQLAlchemy connection pool sizing. Single-replica today: defaults
    # are safe. Multi-replica future (HPA): each replica gets its own
    # pool, so total concurrent DB connections = replicas * (db_pool_size
    # + db_max_overflow). Keep the sum well under the managed DB's
    # max_connections cap. Override via env vars when scaling horizontally.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Auth
    jwt_secret_key: str = "change-me-generate-a-real-secret"
    jwt_access_token_expire_minutes: int = 15
    jwt_algorithm: str = "HS256"
    # Session TTL (days) — drives the refresh cookie ``Max-Age``, the
    # refresh JWT ``exp`` claim, the Redis primary-key TTL, AND the
    # absolute-lifetime check. Single TTL since the 2026-05-18 session-
    # stability refactor: the previous split between ``refresh_idle_ttl_days``
    # and ``session_lifetime_days`` left the org-configurable setting
    # decorative for any value above the idle TTL. Now a per-org override
    # via ``OrgSetting(key="session_lifetime_days", value=…)`` extends or
    # shortens the cookie, the JWT, and the absolute check in lockstep.
    # System default applies when no org override exists.
    session_lifetime_days: int = 30

    # Cookies — True in production (HTTPS), False in dev (HTTP)
    cookie_secure: bool = True

    # Redis (optional — used for sessions/cache in production)
    redis_url: str = ""

    # Email (Mailgun)
    mailgun_api_key: str = ""
    mailgun_domain: str = ""
    mailgun_region: str = ""  # "eu" for EU endpoint, empty for US
    email_from: str = "The Better Decision <noreply@thebetterdecision.com>"
    app_url: str = "http://localhost"  # used for email links

    # MFA
    mfa_encryption_key: str = ""  # Fernet key for encrypting TOTP secrets

    # Google SSO
    google_client_id: str = ""
    google_client_secret: str = ""

    # CORS
    backend_cors_origins: str = "http://localhost:3000"

    # Billing
    default_plan_slug: str = "pro"  # "pro" during beta, "free" when billing goes live
    trial_duration_days: int = 14

    @field_validator("session_lifetime_days")
    @classmethod
    def _validate_session_lifetime_days(cls, v: int) -> int:
        if not (1 <= v <= 365):
            raise ValueError(
                "SESSION_LIFETIME_DAYS must be between 1 and 365 (inclusive)."
            )
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def _validate_jwt_secret(cls, v: str) -> str:
        if v == "change-me-generate-a-real-secret":
            raise ValueError(
                "JWT_SECRET_KEY must be set to a real secret, not the placeholder. "
                "Generate one via: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
            )
        if len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",")]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
