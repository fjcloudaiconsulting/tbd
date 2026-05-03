export interface User {
  id: number;
  username: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  phone: string | null;
  avatar_url: string | null;
  email_verified: boolean;
  role: "owner" | "admin" | "member";
  org_id: number;
  org_name: string;
  billing_cycle_day: number;
  is_superadmin: boolean;
  is_active: boolean;
  mfa_enabled: boolean;
  subscription_status: SubscriptionStatus | null;
  subscription_plan: string | null;
  trial_end: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface MfaChallengeResponse {
  mfa_required: boolean;
  mfa_token: string;
}

export interface MfaSetupResponse {
  qr_code: string;
  secret: string;
  uri: string;
}

export interface MfaEnableResponse {
  recovery_codes: string[];
}

export interface ApiError {
  detail: string;
}

export interface AccountType {
  id: number;
  name: string;
  slug: string | null;
  is_system: boolean;
  account_count: number;
}

export interface Account {
  id: number;
  name: string;
  account_type_id: number;
  account_type_name: string;
  account_type_slug: string | null;
  balance: number;
  currency: string;
  is_active: boolean;
  close_day: number | null;
  is_default: boolean;
}

export interface Category {
  id: number;
  name: string;
  type: "income" | "expense" | "both";
  parent_id: number | null;
  parent_name: string | null;
  description: string | null;
  slug: string | null;
  is_system: boolean;
  transaction_count: number;
}

export interface Transaction {
  id: number;
  account_id: number;
  account_name: string;
  category_id: number;
  category_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  status: "settled" | "pending";
  linked_transaction_id: number | null;
  recurring_id: number | null;
  date: string;
  settled_date: string | null;
  is_imported: boolean;
}

export interface RecurringTransaction {
  id: number;
  account_id: number;
  account_name: string;
  category_id: number;
  category_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  frequency: "weekly" | "biweekly" | "monthly" | "quarterly" | "yearly";
  next_due_date: string;
  auto_settle: boolean;
  is_active: boolean;
}

export interface Budget {
  id: number;
  category_id: number;
  category_name: string;
  amount: number;
  spent: number;
  remaining: number;
  percent_used: number;
  period_start: string;
  period_end: string;
}

export interface BillingPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
}

export interface OrgSetting {
  key: string;
  value: string;
}

export interface ForecastPlanItem {
  id: number;
  plan_id: number;
  category_id: number;
  category_name: string;
  parent_id: number | null;
  type: "income" | "expense";
  planned_amount: number;
  source: "manual" | "recurring" | "history";
  actual_amount: number;
  variance: number;
}

export interface ForecastPlan {
  id: number;
  billing_period_id: number;
  period_start: string;
  period_end: string | null;
  status: "draft" | "active";
  total_planned_income: number;
  total_planned_expense: number;
  total_actual_income: number;
  total_actual_expense: number;
  items: ForecastPlanItem[];
}

// ── Import ──────────────────────────────────────────────────────────────────

export type SuggestionSource = "org_rule" | "shared_dictionary" | "default";

export interface ImportPreviewRow {
  row_number: number;
  date: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  counterparty: string | null;
  transaction_type: string | null;

  // Existing duplicate-detection (different from transfer-leg duplicate)
  is_duplicate: boolean;
  duplicate_transaction_id: number | null;

  // Smart-rules suggestion
  suggested_category_id?: number | null;
  suggestion_source?: SuggestionSource | null;

  // Detector 1: matches an already-linked leg on the same account → drop default
  is_duplicate_of_linked_leg: boolean;
  duplicate_candidate?: DuplicateCandidate | null;
  default_action_drop: boolean;

  // Detector 2: cross-account un-linked match (transfer-pair candidate)
  transfer_match_action: "none" | "pair_with" | "suggest_pair" | "choose_candidate";
  transfer_match_confidence?: "same_day" | "near_date" | "multi_candidate" | null;
  pair_with_transaction_id?: number | null;
  transfer_candidates: TransferCandidate[];
}

export interface ImportPreviewResponse {
  rows: ImportPreviewRow[];
  account_id: number;
  file_name: string;
  total_rows: number;
  duplicate_count: number;

  // New per-spec §3.2 summary counters (replace transfer_candidate_count)
  auto_paired_count: number;
  suggested_pair_count: number;
  multi_candidate_count: number;
  duplicate_of_linked_count: number;
}

export interface ImportConfirmRow {
  row_number: number;
  date: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  category_id: number | null;
  skip: boolean;
  // Spec §3.2 confirm-row action mapping
  action?: "create" | "pair_with_existing" | "drop_as_duplicate";
  pair_with_transaction_id?: number | null;
  duplicate_of_transaction_id?: number | null;
  transfer_category_id?: number | null;
  recategorize?: boolean;
  // Echoed from preview for accept-vs-override smart-rules detection
  suggested_category_id?: number | null;
  suggestion_source?: SuggestionSource | null;
}

export interface ImportConfirmRequest {
  account_id: number;
  default_category_id: number;
  rows: ImportConfirmRow[];
}

export interface ImportRowError {
  row_number: number;
  error: string;
}

export interface ImportConfirmResponse {
  imported_count: number;
  paired_count: number;
  dropped_duplicate_count: number;
  skipped_count: number;
  error_count: number;
  errors: ImportRowError[];
}

export interface Plan {
  id: number;
  name: string;
  slug: string;
  description: string;
  is_custom: boolean;
  is_active: boolean;
  sort_order: number;
  price_monthly: number;
  price_yearly: number;
  max_users: number | null;
  retention_days: number | null;
  features: PlanFeatures;

  // CLEANUP-029: remove the three fields below when migration 029 ships.
  ai_budget_enabled: boolean;
  ai_forecast_enabled: boolean;
  ai_smart_plan_enabled: boolean;
}

export type SubscriptionStatus = "trialing" | "active" | "past_due" | "canceled";

export interface SubscriptionDetail {
  id: number;
  org_id: number;
  plan: Plan;
  status: SubscriptionStatus;
  billing_interval: "monthly" | "yearly";
  trial_start: string | null;
  trial_end: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
}

// L4.11 feature entitlements -------------------------------------------------

export type FeatureKey =
  | "ai.budget"
  | "ai.forecast"
  | "ai.smart_plan"
  | "ai.autocategorize";

export interface PlanFeatures {
  "ai.budget": boolean;
  "ai.forecast": boolean;
  "ai.smart_plan": boolean;
  "ai.autocategorize": boolean;
}

export interface OrgFeatureOverride {
  feature_key: FeatureKey;
  value: boolean;
  set_by: number | null;
  set_by_email: string | null;
  set_at: string;          // ISO 8601 UTC
  expires_at: string | null;  // ISO 8601 UTC
  note: string | null;
  is_expired: boolean;
}

export interface FeatureStateRow {
  key: FeatureKey;
  plan_default: boolean;
  effective: boolean;
  override: OrgFeatureOverride | null;
}

export interface FeatureStateResponse {
  plan: { id: number; name: string; slug: string } | null;
  features: FeatureStateRow[];
}

// ── Transfer-pair shapes ─────────────────────────────────────────────────────

export interface TransferCandidate {
  id: number;
  date: string;
  description: string;
  amount: number;
  account_id: number;
  account_name: string;
  date_diff_days: number;
  confidence: "same_day" | "near_date";
}

export interface TransferCandidatesResponse {
  candidates: TransferCandidate[];
}

export interface DuplicateCandidate {
  id: number;
  date: string;
  description: string;
  amount: number;
  account_id: number;
  account_name: string;
  existing_leg_is_imported: boolean;
}

export interface TransactionPairRequest {
  expense_id: number;
  income_id: number;
  transfer_category_id?: number | null;
  recategorize?: boolean;
}

export interface ConvertToTransferRequest {
  destination_account_id: number;
  pair_with_transaction_id?: number | null;
  transfer_category_id?: number | null;
  recategorize?: boolean;
}

export interface UnpairTransactionRequest {
  expense_fallback_category_id: number;
  income_fallback_category_id: number;
}
