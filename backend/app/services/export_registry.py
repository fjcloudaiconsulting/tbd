"""Per-table export dispositions for the org data export (TBD-222).

**This module is a decision record, not a helper.** Every table in
``Base.metadata`` gets exactly one hand-written entry saying whether it
belongs in a data-subject export and, if so, how it is scoped to an org
and which of its columns are withheld.

Why a literal dict and not a derived rule
-----------------------------------------
``org_data_service.wipe_org_data`` carries a docstring promising *"every
new org-scoped data table goes through this function"* and covers **15 of
49** tables. A convention in a docstring is not a mechanism. The guard
here is a *failing test*: ``tests/services/test_export_registry.py`` asserts
this dict and the runtime SQLAlchemy metadata are the same set, in both
directions, so adding a model without deciding its disposition breaks CI.

The two sides are deliberately independent. ``Base.metadata`` is produced
by class creation — authored by whoever adds a model, who by hypothesis is
not thinking about the export. ``EXPORT_DISPOSITION`` is typed by a human
who is.

Disposition rule (spec §5)
--------------------------
    Include iff (provided by the subject) OR (readable by an org OWNER
    through the app). Exclude otherwise.

A union, deliberately. Owner-readability alone collapses GDPR Art. 15 into
Art. 20 and would drop ``feedback_entries``, which the subject typed in.
Provided-by alone would drop things an owner already reads on screen.

Redaction is opt-OUT (spec §6)
------------------------------
A new column on ``transactions`` lands in the export automatically. That is
correct: the drift this subsystem exists to prevent is *omission*. Columns
are withheld only by naming them in an ``Include.redact`` set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


# Bumped whenever a disposition, a scope, or a redact set changes. Recorded
# in the export header and in the ``org.data.exported`` audit row so an
# artifact handed to a data subject can be tied to the rules that built it.
REGISTRY_VERSION = 1


# ── Scoping ────────────────────────────────────────────────────────────────
#
# ⚠ ``org_id`` is NOT a sufficient predicate (spec §4 leg 4). Six included
# tables reach the org only by join, and two more have a NULLABLE ``org_id``.
# A blanket ``WHERE org_id = ?`` would silently emit zero rows for the former
# and drop rows for the latter. Scope is therefore declared per table.


@dataclass(frozen=True)
class OrgColumn:
    """Scope by an org-valued column on the table itself.

    ``column`` is not always ``org_id``: ``organizations`` scopes on its own
    ``id``, and ``audit_events`` scopes on ``target_org_id``.
    """

    column: str = "org_id"


@dataclass(frozen=True)
class Via:
    """Scope by membership in a parent table's org-scoped id set.

    Emits ``WHERE <local_column> IN (SELECT <parent>.id FROM <parent> WHERE
    <parent scope>)``. The parent's own disposition supplies the inner
    predicate, so a scope fix on the parent propagates here for free.
    """

    parent_table: str
    local_column: str


Scope = Union[OrgColumn, Via]


# ── Dispositions ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Include:
    """This table's rows belong in the export."""

    scope: Scope
    reason: str
    redact: frozenset[str] = field(default_factory=frozenset)

    @property
    def included(self) -> bool:
        return True


@dataclass(frozen=True)
class Exclude:
    """This table's rows do NOT belong in the export.

    ``reason`` ships in the export header's ``excluded`` block, so a data
    subject can see what was withheld and why rather than inferring absence.
    """

    reason: str

    @property
    def included(self) -> bool:
        return False


Disposition = Union[Include, Exclude]


# ── Redaction ──────────────────────────────────────────────────────────────

# Column-name heuristic for the drift fence (spec §6). This is a SECOND,
# independent source of truth about which columns are dangerous — derived
# from naming, not from the redact sets above. A column on an included table
# matching any of these must be either redacted or explicitly allowlisted.
SECRET_NAME_PATTERNS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "hash",
    "encrypted",
    "recovery",
    "credential",
    "cipher",
    "salt",
    "nonce",
    "private_key",
    "api_key",
    "passphrase",
)

# ⚠ A name heuristic has a known blind spot in BOTH directions.
#
# False negatives: ``org_ai_credentials.base_url`` is a String(512) that can
# carry ``https://user:pass@host`` in band and matches NO pattern above. Its
# redaction is stated explicitly in the redact set, not left to this fence.
#
# False positives: the entries below. Each needs a one-line justification.
SECRET_NAME_ALLOWLIST: dict[tuple[str, str], str] = {
    ("ai_usage_ledger", "credential_id"): "FK id, not the credential itself",
    ("ai_usage_ledger", "prompt_tokens"): "LLM token COUNT, an integer",
    ("ai_usage_ledger", "completion_tokens"): "LLM token COUNT, an integer",
    ("ai_usage_ledger", "total_tokens"): "LLM token COUNT, an integer",
    ("category_rules", "normalized_token"): (
        "normalized merchant substring the user typed as a rule; the "
        "subject's own categorisation data, not a credential"
    ),
    ("org_ai_default_routing", "credential_id"): "FK id, not the credential",
    ("org_ai_feature_routing", "credential_id"): "FK id, not the credential",
    # ⚠ Both of these ARE exported, deliberately (spec §6). They are
    # datetimes, not credentials, and they are the subject's own security
    # history — Art. 15 material. An earlier brief wrongly forbade them.
    ("users", "password_changed_at"): "datetime, the subject's security history",
    ("users", "password_set"): "boolean flag, not a credential",
}


# ── The 49 dispositions ────────────────────────────────────────────────────
#
# Kept in alphabetical order so a reviewer can diff it against
# ``sorted(Base.metadata.tables)`` by eye.

EXPORT_DISPOSITION: dict[str, Disposition] = {
    "account_types": Include(OrgColumn(), "org-scoped account taxonomy the org defined"),
    "accounts": Include(OrgColumn(), "the subject's accounts"),
    "ai_usage_ledger": Include(
        OrgColumn(),
        # ⚠ NOT "behavioural data about identifiable users": this table has
        # no user_id column, so there is no user attribution to be had. A
        # wrong reason in the registry documents a falsehood.
        "org-scoped, no third-party surface",
    ),
    "announcements": Exclude("platform-global operator content, identical for every org"),
    # ⚠ Reason strings SHIP to the data subject in the header's ``excluded``
    # block, so a wrong one is a statement made to a third party. PATs are
    # not user-scoped: every route on ``/api/v1/api-tokens`` is gated by
    # ``require_superadmin`` AND ``require_interactive_session``.
    "api_tokens": Exclude(
        "superadmin-only platform credential with no org_id; token_hash is a "
        "live capability, not org data"
    ),
    # ⚠ Scope is target_org_id ONLY. NEVER add an actor_user_id disjunct: a
    # superadmin's admin.* actions against OTHER tenants carry their
    # actor_user_id with a foreign target_org_id, so a disjunct would export
    # other tenants' audit rows.
    "audit_events": Include(
        OrgColumn("target_org_id"),
        "actions taken on this org, readable by an owner at /admin/audit",
        # ip_address: a household partner's IP history is the Art. 20(4)
        # harm. detail: free-form JSON — a field allowlist inside a blob is
        # unauditable, so the whole column goes.
        #
        # ⚠ actor_email is dropped UNCONDITIONALLY, and that is a ruling, not
        # an oversight. ~20 writers (admin_orgs, admin_features,
        # admin_subscriptions, admin_rate_limit_overrides, ...) set
        # ``actor_email = current_user.email`` for a SUPERADMIN whose own
        # ``users.org_id`` is a different org, while ``target_org_id`` is the
        # tenant. Exporting it therefore hands an operator's work email to a
        # data subject — the same Art. 15(4) third-party reasoning that drops
        # ``ip_address``.
        #
        # A row-dependent rule ("keep it only where actor_user_id is one of
        # this org's users") was considered and REJECTED as unimplementable:
        # ``Include.redact`` is a static column-name set consumed by
        # ``_exported_columns`` to build the SELECT, so there is no per-row
        # hook, and inventing one would put a second scoping mechanism beside
        # ``scope_predicate``. Nothing is lost for the org's own members:
        # ``actor_user_id`` IS exported and joins to the exported ``users``
        # rows, which carry ``email``.
        #
        # ⚠ api_token_id (added by TBD-188, PR #635) is dropped for the SAME
        # reason as ``actor_email`` — NOT because a token id is secret. It is
        # not: ``models/api_token.py`` states ``token_hash`` is the stored
        # credential and ``token_prefix`` is "a short, non-secret slice".
        # PATs are superadmin-only platform credentials, and ``api_tokens`` is
        # EXCLUDED from this export as "superadmin-only platform credential
        # with no org_id". So an ``api_token_id`` on an audit row identifies
        # WHICH OPERATOR TOKEN acted on this tenant, and points into a table
        # the subject cannot see. Contrast ``actor_user_id``, which IS
        # exported precisely because it joins to the exported ``users`` rows.
        #
        # This column arrived from ``main`` after this branch was cut. The
        # column-drift fence caught it in CI and forced an explicit decision
        # rather than letting a new secret-named column export silently —
        # which is exactly what that fence exists to do.
        redact=frozenset({"ip_address", "detail", "actor_email", "api_token_id"}),
    ),
    "billing_periods": Include(OrgColumn(), "the subject's billing periods"),
    "budgets": Include(OrgColumn(), "the subject's budgets"),
    "categories": Include(OrgColumn(), "the subject's category tree"),
    "category_rules": Include(OrgColumn(), "categorisation rules the subject wrote"),
    # No org_id column — org isolation is by the parent account.
    "cc_cycle_payments": Include(
        Via("accounts", "account_id"), "per-cycle CC payments on the subject's accounts"
    ),
    "dashboard_layouts": Include(OrgColumn(), "dashboards the subject arranged"),
    # ⚠ No org_id. An unfiltered read here is a full-platform address book.
    "email_broadcast_recipients": Include(
        Via("users", "user_id"),
        "delivery record of platform email sent to this org's users",
        # A Mailgun provider diagnostic — leaks our infrastructure, not
        # their data.
        redact=frozenset({"error"}),
    ),
    "email_broadcasts": Exclude("platform-global operator campaign, not org data"),
    # Nullable org_id: anonymous feedback has org_id IS NULL and is
    # correctly excluded by the equality predicate.
    "feedback_entries": Include(OrgColumn(), "feedback the subject typed in"),
    "forecast_plan_items": Include(OrgColumn(), "the subject's plan line items"),
    "forecast_plans": Include(OrgColumn(), "the subject's forecast plans"),
    "import_batches": Include(OrgColumn(), "the subject's import history"),
    # The org typed these addresses in, and MembersSection already renders
    # pending invites to admins. No token column exists on this table, so a
    # full export hands over no live capability.
    "invitations": Include(OrgColumn(), "addresses the org typed in; already shown in-app"),
    "merchant_dictionary": Exclude("cross-org PUBLIC normalisation dictionary, not org data"),
    "notifications": Include(
        Via("users", "user_id"), "in-app notifications the subject received"
    ),
    "org_ai_consents": Include(OrgColumn(), "consent decisions the org recorded"),
    "org_ai_credentials": Include(
        OrgColumn(),
        "AI provider configuration the org created",
        # ⚠ ``last_four`` IS exported, deliberately. It is partial key
        # material, but it is the ORG'S OWN key, it is already rendered
        # masked in-app on the AI settings screen, and it is not enough to
        # authenticate anything. It therefore passes the §5 owner-readable
        # rule and withholding it would omit something the subject can
        # already see. Recorded here so the decision is not re-litigated as
        # an oversight.
        # ⚠ base_url matches NO secret-name pattern but is a String(512)
        # that can carry https://user:pass@host in band.
        redact=frozenset(
            {"encrypted_api_key", "encrypted_bearer_token", "key_fingerprint", "base_url"}
        ),
    ),
    "org_ai_default_caps": Include(OrgColumn(), "spend caps the org set"),
    "org_ai_default_routing": Include(OrgColumn(), "provider routing the org set"),
    "org_ai_feature_caps": Include(OrgColumn(), "per-feature spend caps the org set"),
    "org_ai_feature_routing": Include(OrgColumn(), "per-feature routing the org set"),
    "org_data_reset_locks": Exclude(
        "operator/runtime lease; lease_token is a live capability, not data"
    ),
    "org_feature_overrides": Exclude("operator configuration about the org, not the org's data"),
    "org_settings": Include(OrgColumn(), "settings the subject chose"),
    "organizations": Include(OrgColumn("id"), "the org record itself"),
    "plans": Exclude("platform-global price list"),
    "rate_limit_overrides": Exclude(
        "operator configuration (nullable org_id, operator-authored note)"
    ),
    "recurring_transactions": Include(OrgColumn(), "the subject's recurring templates"),
    "report_versions": Include(
        Via("reports", "report_id"), "version history of the subject's reports"
    ),
    "reports": Include(OrgColumn(), "reports the subject built"),
    "role_permissions": Exclude("platform-global RBAC definition"),
    "roles": Exclude("platform-global RBAC definition"),
    "scenarios": Include(OrgColumn(), "what-if scenarios the subject built"),
    "subscriptions": Include(OrgColumn(), "the org's subscription record"),
    "system_settings": Exclude("platform-global operator configuration"),
    "tag_dictionary": Exclude("cross-org PUBLIC k-anonymous dictionary, not org data"),
    # ⚠ The one table where exporting would be an ACTIVE privacy breach.
    # Its own docstring: never read by any API endpoint, never serialized.
    # Emitting contributor_org_id rows is precisely the de-anonymisation the
    # k-anonymity design exists to prevent.
    "tag_dictionary_contributors": Exclude(
        "cross-org PRIVATE k-anonymity substrate; exporting it de-anonymises "
        "other orgs' contributions"
    ),
    "tags": Include(OrgColumn(), "tags the subject created"),
    # ⚠ No org_id. The lazy fix for that is to skip the filter, which would
    # emit every tag link on the platform.
    "transaction_tags": Include(
        Via("transactions", "transaction_id"), "tag links on the subject's transactions"
    ),
    "transactions": Include(OrgColumn(), "the subject's transactions"),
    "user_dismissed_announcements": Include(
        Via("users", "user_id"), "announcements the subject dismissed"
    ),
    "user_notification_preferences": Include(
        Via("users", "user_id"), "notification preferences the subject set"
    ),
    "users": Include(
        OrgColumn(),
        "the subject's own account records",
        redact=frozenset(
            {
                "password_hash",
                "totp_secret",
                "recovery_codes",
                "stepup_token",
                "stepup_token_expires_at",
            }
        ),
    ),
}


def included_tables() -> set[str]:
    """Table names the export emits rows for."""
    return {t for t, d in EXPORT_DISPOSITION.items() if d.included}


def excluded_reasons() -> dict[str, str]:
    """``{table: reason}`` for every withheld table — ships in the header."""
    return {t: d.reason for t, d in EXPORT_DISPOSITION.items() if not d.included}
