from pydantic import field_validator, model_validator
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
    # Dedicated HMAC key for MFA recovery-code hashing. Optional: when unset
    # (the default), recovery codes hash under the jwt_secret_key-derived key
    # exactly as before. When set, NEW/regenerated recovery hashes key off this
    # secret instead, so rotating jwt_secret_key no longer invalidates them.
    # See specs/mfa-recovery-hmac-key-decouple.md. This is an operational
    # rotation-decoupling knob, not a key-separation upgrade — purpose
    # derivation already separates recovery codes from JWT signing.
    mfa_recovery_hmac_key: str = ""
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
    # Connect-time SSRF guard escape hatch for the ollama provider ONLY.
    # By default every outbound request to an org-configured base_url
    # resolves the hostname, validates EVERY A/AAAA record against the
    # egress denylist (loopback, RFC1918/ULA, link-local, metadata,
    # multicast, reserved, non-global), and pins the connection to a
    # validated IP (see services/ai_providers/egress_guard.py). A
    # self-hosted operator who genuinely runs Ollama on a private or
    # loopback address can set AI_PROVIDER_ALLOW_PRIVATE_NETWORKS=1 to
    # permit private + loopback targets for ollama credentials.
    # Link-local / cloud-metadata / multicast / reserved addresses stay
    # blocked regardless, and openai_compatible always gets the full
    # denylist. Default OFF in all environments.
    ai_provider_allow_private_networks: bool = False
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
    # NOTE: this bounds a SINGLE provider attempt, not the whole dispatch.
    # ``call_llm_structured`` wraps each attempt in ``_with_dispatch_timeout``
    # *inside* the retry loop (``STRUCTURED_OUTPUT_MAX_RETRIES`` + 1 = up to
    # 3 attempts), so the worst-case wall clock for a structured dispatch is
    # ~3x this value (~90 s at 30 s). That still sits under DO/Cloudflare's
    # 100 s origin-response ceiling, so the request is not truncated upstream.
    #
    # The original 5 s ceiling proved too tight in production: a single
    # structured-output completion for a real provider routinely exceeds
    # 5 s, so every attempt flat-lined at ~5000 ms and all AI features
    # returned the "temporarily unavailable" empty state. 30 s/attempt
    # covers a real round-trip while still bounding a hung provider well
    # under the adapter's coarse read budget. Tunable via
    # ``AI_DISPATCH_TIMEOUT_S``.
    ai_dispatch_timeout_s: float = 30.0

    # Google SSO
    google_client_id: str = ""
    google_client_secret: str = ""

    # CORS
    backend_cors_origins: str = "http://localhost:3000"

    # Founding-members program (2026-06-22).
    # Throttle for the per-user last_active_at stamp: only re-stamp when the
    # stored value is older than this many seconds (≤1 write/user/window).
    last_active_stamp_throttle_seconds: int = 3600
    # Usernames excluded from the public founder count (smoke/seed accounts).
    # CSV, mirrors the cors-origins parsing pattern.
    founder_count_exclude_usernames: str = "pfv_smoke_l05"

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

    # Plans (forecast-driven plan builder)
    # When ``feature_plans`` is False (the pre-launch default), the
    # ``/api/v1/scenarios/*`` router-level dependency ``require_feature``
    # raises a hard 404 on every route and the frontend hides the nav
    # item. Flip to True (or override via SystemSetting / OrgSetting)
    # once the frontend lands.
    feature_plans: bool = False

    # Custom Dashboard (W4 customizable dashboard — gridstack.js canvas)
    # Ships ON by default (global flip). The ``/api/v1/dashboard/*`` router
    # and the frontend nav resolve through the feature gate, so a per-org
    # OrgSetting of ``"off"`` (or a global SystemSetting of ``"off"``) is the
    # supported rollback: it flips the flag off for that org and the frontend
    # falls back to LegacyDashboard, which is kept for exactly this reason.
    feature_custom_dashboard: bool = True

    # Scheduled Tasks (recurring org operations — billing-period close, recurring transactions, etc.)
    # When ``scheduler_enabled`` is False, the tick loop does not run and no
    # scheduled tasks execute. When True, the tick loop runs with an interval
    # of ``scheduler_tick_seconds`` and acquires a distributed lock with TTL
    # ``scheduler_lock_ttl_seconds`` to prevent concurrent runs across replicas.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 900
    scheduler_lock_ttl_seconds: int = 600
    # Rollout guard: cap how many orgs may perform real work (a job that closes a
    # billing period, generates recurring transactions, or emails members) in a
    # single tick. A fresh deploy or long-downtime catch-up would otherwise mutate
    # and email every org at once; the cap drains the backlog across ticks instead
    # (skipped orgs stay due and are picked up next tick). Set to 0 (or any value
    # <= 0) for no cap (pre-guard burst behavior).
    scheduler_max_orgs_per_tick: int = 25

    # OFX statement-import parsing isolation + concurrency (DoS mitigation).
    # OFX files are parsed in a hard-killable child process
    # (``app.services.import_ofx_service``): on timeout the process is
    # terminated so a pathological / adversarial file cannot pin a CPU core
    # after the request returns. These knobs bound the blast radius on the
    # single-replica prod box. Each replica runs its own executor + child
    # processes (compute-only isolation), so the caps are per-replica and
    # horizontally-scale-safe.
    #
    # ``ofx_parse_max_concurrent``: global ceiling on simultaneous OFX
    #   parses (child processes) in this process.
    # ``ofx_parse_max_per_org``: per-org ceiling — one org cannot occupy
    #   every slot. Over-cap → HTTP 429 immediately.
    # ``ofx_parse_queue_wait_s``: bounded wait for a free GLOBAL slot before
    #   returning 429 (smooths transient bursts; 0 = reject immediately).
    # ``ofx_parse_timeout_s``: wall-clock parse budget; on overrun the child
    #   is terminated and the request gets HTTP 400.
    # ``ofx_max_rows``: post-parse transaction cap → HTTP 413. Restored to
    #   10 000 now that parsing is isolated + killable (was temporarily
    #   lowered to 2 000 as a pre-isolation DoS stopgap).
    ofx_parse_max_concurrent: int = 4
    ofx_parse_max_per_org: int = 2
    ofx_parse_queue_wait_s: float = 5.0
    ofx_parse_timeout_s: float = 10.0
    ofx_max_rows: int = 10_000

    # Superadmin email broadcast (spec 2026-07-18).
    # ``broadcast_max_recipients``: hard backstop cap — POST /{id}/send
    # refuses a recomputed segment count above this outright, guarding
    # against a segment-query bug blasting far more people than plausible.
    # ``broadcast_pacing_seconds``: sleep between sends in the drain loop.
    # ``broadcast_max_attempts``: ``resume`` only retries recipient rows
    # with ``attempts`` below this, so a permanently bad address isn't
    # hammered on every resume.
    # ``broadcast_batch_size``: recipients per Mailgun batch-sending call
    # (2026-07-19 batch-sending revision, MA6). Hard-capped at 1000
    # (Mailgun's per-call limit) where the value is consumed by the drain,
    # not here — this default is already at the cap.
    broadcast_max_recipients: int = 10000
    broadcast_pacing_seconds: float = 1.0
    broadcast_max_attempts: int = 3
    broadcast_batch_size: int = 1000

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

    @model_validator(mode="after")
    def _validate_mfa_recovery_hmac_key(self) -> "Settings":
        # Optional: empty (or whitespace-only) is the backward-compatible no-op
        # path — recovery codes keep hashing under the jwt-derived key. When a
        # real value is provided, validate it. Cross-field (needs
        # jwt_secret_key), so this is a model-level validator.
        key = self.mfa_recovery_hmac_key.strip()
        # Normalize the stored value so downstream truthiness ("is it set?")
        # can't be fooled by whitespace-only input.
        self.mfa_recovery_hmac_key = key
        if not key:
            return self
        if len(key) < 32:
            raise ValueError("MFA_RECOVERY_HMAC_KEY must be at least 32 characters")
        if key == self.jwt_secret_key:
            raise ValueError(
                "MFA_RECOVERY_HMAC_KEY must differ from JWT_SECRET_KEY — reusing "
                "the JWT secret re-couples recovery-code hashing to it and "
                "defeats the decoupling. Generate a distinct secret via: "
                "python -c 'import secrets; print(secrets.token_urlsafe(64))'"
            )
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",")]

    @property
    def founder_count_exclude_list(self) -> list[str]:
        return [
            u.strip()
            for u in self.founder_count_exclude_usernames.split(",")
            if u.strip()
        ]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
