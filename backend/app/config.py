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

    # Pool recycle MUST stay under the App Platform → droplet VPC's
    # idle-connection drop interval. Once a pooled connection sits
    # idle longer than that, the kernel TCP socket on the App-side is
    # dead but the pool doesn't know — pre_ping fires, blocks reading
    # from the dead socket until the kernel TCP RTO (tens of seconds),
    # and every endpoint that touches the DB hangs. 280 s is a
    # conservative ceiling well under the typical 300 s NAT timeout.
    db_pool_recycle: int = 280

    # connect_timeout is passed through to ``aiomysql.connect()`` and
    # bounds initial handshake. Sized for cold-start under transient
    # VPC blips — too tight breaks legitimate slow connects without
    # buying back user-visible latency.
    #
    # Per-operation read/write timeouts are NOT exposed here:
    # aiomysql 0.2.0 (pinned in requirements.txt) doesn't accept
    # them. Stale-socket bounds live at ``db_pool_recycle`` (rotate
    # before NAT drop) and at the route-local handler timeout.
    db_connect_timeout: int = 10

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

    # Auth diagnostic logging. When True, ``/auth/refresh`` emits a
    # structured ``auth.refresh.rejected`` event at every terminal-401
    # raise site with a stable ``reason`` enum and 8-char hash prefixes
    # of jti/sid (PII guard — raw values never leave the process).
    # Default OFF in production to keep INFO-level logs quiet under
    # normal operation; flip to True during incident triage and back
    # off once the diagnosis is in hand. Does NOT gate the warn-level
    # ``redis.client.retired`` event — that is a real ops signal worth
    # keeping on regardless.
    auth_debug_logging: bool = False

    # Absolute ceiling on the wall-clock time the ``/auth/refresh``
    # handler may spend before the route returns 503. The honest
    # worst-case Redis budget for the deepest /refresh branch is
    # ~22 s (see ``redis_client._build_auth_redis_client`` docstring);
    # this ceiling sits above that so normal slow paths still
    # complete, and below the frontend's 45 s reactive-recovery
    # abort so a wedged handler always surfaces as a clean 503 the
    # browser can retry on instead of a silent hang with no log.
    refresh_handler_timeout_s: float = 25.0

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

    # AI credentials (BYO provider keys, PR1 of AI tier train)
    # Fernet key for encrypting per-org provider API keys in
    # ``org_ai_credentials.encrypted_api_key`` / ``encrypted_bearer_token``.
    # MUST be a different Fernet key from ``mfa_encryption_key`` — the
    # lifespan KEK guard refuses to boot when the two SHA-256 hashes match.
    # ``_PREV`` is the previous-rotation key; decrypt falls back to it
    # when the current key fails, enabling lazy rotation in place.
    ai_credential_encryption_key: str = ""
    ai_credential_encryption_key_prev: str = ""
    # Master gate for the native (server-hosted) provider option. Stays
    # OFF in PR1 — flipped on later when the native adapter ships
    # alongside the consent UI (PR4).
    ai_native_enabled: bool = False
    # Pinned ToS version for the native-provider consent flow. POSTs to
    # /api/v1/settings/ai-providers/consent must carry this exact
    # consent_version string — any mismatch (older OR newer) returns
    # 400 code=consent_version_outdated. Bump this value when shipping
    # a new ToS to force every org to re-consent on the next admin-UI
    # mount; existing consent rows are never auto-upgraded (spec §3.5).
    # The string is included in the error response so the frontend can
    # re-prompt with the current version.
    ai_native_current_consent_version: str = "ai-tos-2026-05-22"

    # Wall-clock bound on a single LLM dispatch call inside
    # ``ai_dispatch``. The per-provider HTTP adapters carry their own
    # coarse connect/read timeouts (10 s validate, 30-60 s chat), but a
    # slow or hung provider can still pin a dispatch worker for the full
    # adapter budget. This tighter wall-clock ceiling wraps every adapter
    # call in ``asyncio.wait_for`` so a single call can never exceed it.
    # On timeout the dispatcher records a system-failure ledger row and
    # raises ``AIDispatchFailed("provider_timeout")`` (5xx). Architect-
    # mandated reliability bound (AI tier decision, 2026-05-22).
    #
    # The original 5 s ceiling proved too tight in production: a full
    # structured-output completion (plus up to two schema retries) for a
    # real provider routinely exceeds it, so every dispatch flat-lined at
    # ~5000 ms and all AI features returned the "temporarily unavailable"
    # empty state. 30 s covers a real round-trip while still bounding a
    # hung provider well under the adapter's coarse read budget. Tunable
    # via ``AI_DISPATCH_TIMEOUT_S``.
    ai_dispatch_timeout_s: float = 30.0

    # Google SSO
    google_client_id: str = ""
    google_client_secret: str = ""

    # CORS
    backend_cors_origins: str = "http://localhost:3000"

    # Billing
    default_plan_slug: str = "pro"  # "pro" during beta, "free" when billing goes live
    trial_duration_days: int = 14

    # CAPTCHA (registration bot gate)
    # When ``captcha_required`` is True the ``/api/v1/auth/register`` handler
    # calls ``app.captcha.verify_captcha`` BEFORE any DB work or email send,
    # and refuses registration on any non-OK result (rejection, timeout,
    # mismatch). When False the verify call short-circuits to ok=disabled and
    # registration proceeds without a captcha token.
    #
    # Flipping ``captcha_required`` is the rollback path during a provider
    # outage — frontend reads the same flag from ``/api/v1/auth/status`` so a
    # backend flip-to-False also drops the widget-render gate on the next
    # page load.
    #
    # ``captcha_secret`` is provider-specific (Cloudflare Turnstile siteverify
    # secret today). Never logged. Provider test secret
    # ``1x0000000000000000000000000000000AA`` always returns success and is
    # safe in dev / CI.
    captcha_required: bool = False
    captcha_provider: str = "turnstile"
    captcha_secret: str = ""
    captcha_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    captcha_verify_timeout_s: float = 5.0
    # Optional defense-in-depth pins. Empty string disables the check (the
    # provider's domain allowlist on the widget still applies).
    captcha_expected_hostname: str = ""
    captcha_expected_action: str = ""

    # Billing UI (customer-facing plan / trial / billing surface)
    # When ``billing_ui_enabled`` is False (the pre-payment default), the
    # customer-facing billing surface is hidden: the trial banner returns
    # null, the settings Billing tab is filtered out of the nav, and
    # ``/settings/billing`` renders an explanatory empty state instead of
    # the plan grid. Admin / operator views under ``/admin/*`` and
    # ``/system/*`` are unaffected.
    #
    # The flag is exposed via ``/api/v1/auth/status`` (same shape as
    # ``captcha_required``) so a backend flip becomes a real customer-
    # facing change on the next page load. Flip to True when the payment
    # platform is wired.
    #
    # Backend API gating is NOT in scope — ``/api/v1/subscriptions`` and
    # ``/api/v1/plans`` stay reachable; the gated frontend components just
    # don't call them when the flag is off.
    billing_ui_enabled: bool = False

    # Reports v2 (flexible canvas + AST query engine)
    # When ``feature_reports_v2`` is False (the pre-launch default), the
    # ``/api/v1/reports/*`` router-level dependency
    # ``require_reports_v2_enabled`` raises a hard 404 on every route,
    # the frontend hides the nav item and routes (gated separately via
    # ``NEXT_PUBLIC_FEATURE_REPORTS_V2``), and the surface is
    # effectively invisible. Flip to True once the frontend lands. See
    # ``specs/2026-05-22-reports-v2-flexible-canvas.md`` §11.
    feature_reports_v2: bool = False

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
