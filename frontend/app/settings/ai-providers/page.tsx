"use client";

import { useCallback, useEffect, useState } from "react";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import Pagination from "@/components/ui/Pagination";
import SortableHeader from "@/components/ui/SortableHeader";
import { useTableState } from "@/lib/hooks/use-table-state";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import type { ListEnvelope } from "@/lib/types";
import {
  btnPrimary,
  btnSecondary,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";

type Provider =
  | "openai"
  | "anthropic"
  | "ollama"
  | "openai_compatible"
  | "native";

interface Credential {
  id: number;
  org_id: number;
  provider: Provider;
  last_four: string | null;
  key_fingerprint: string | null;
  base_url: string | null;
  label: string | null;
  discovered_capabilities: string[] | null;
  discovered_models: string[] | null;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  last_validated_at: string | null;
  validation_error: string | null;
}

interface ProviderOption {
  key: Provider;
  label: string;
  availability: "available" | "not_yet_available";
}

interface ProviderOptionsResponse {
  providers: ProviderOption[];
  ai_native_enabled: boolean;
}

interface DefaultRouting {
  org_id: number;
  credential_id: number;
  model: string;
}

interface FeatureRouting {
  org_id: number;
  feature_name: string;
  credential_id: number;
  model: string;
}

interface RoutingBundle {
  default: DefaultRouting | null;
  features: FeatureRouting[];
}

interface DefaultCaps {
  org_id: number;
  soft_cap_cents: number | null;
  hard_cap_cents: number | null;
  period: string;
}

interface FeatureCaps {
  org_id: number;
  feature_key: string;
  soft_cap_cents: number | null;
  hard_cap_cents: number | null;
  period: string;
}

interface CapsBundle {
  default: DefaultCaps | null;
  features: FeatureCaps[];
}

const PROVIDER_LABELS: Record<Provider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
  openai_compatible: "OpenAI-compatible",
  native: "Native (hosted)",
};

const PROVIDER_DOC_LINKS: Partial<Record<Provider, { href: string; label: string }>> = {
  openai: { href: "https://platform.openai.com/api-keys", label: "Get an OpenAI API key" },
  anthropic: { href: "https://console.anthropic.com/settings/keys", label: "Get an Anthropic API key" },
  ollama: { href: "https://ollama.com/download", label: "Set up Ollama locally" },
  openai_compatible: {
    href: "https://platform.openai.com/docs/api-reference",
    label: "Use an OpenAI-compatible endpoint",
  },
};

// Mirrors backend ROUTABLE_FEATURE_NAMES — keep in sync.
const ROUTABLE_FEATURES: { key: string; label: string }[] = [
  { key: "categorize_transactions", label: "Categorize transactions" },
  { key: "smart_forecast", label: "Smart forecast" },
  { key: "smart_budget", label: "Smart budget" },
  { key: "smart_plan", label: "Smart plan" },
  { key: "chat", label: "Chat" },
];

const NEEDS_BASE_URL: Provider[] = ["ollama", "openai_compatible"];
const ALLOWS_BEARER: Provider[] = ["ollama"];

// Backend-whitelisted sort keys for /api/v1/settings/ai-providers. Limited
// to the UI-exposed sortable columns.
const CREDENTIAL_SORT_FIELDS = ["provider", "label", "created_at"] as const;
type CredentialSortField = (typeof CREDENTIAL_SORT_FIELDS)[number];


export default function AiProvidersPage() {
  const { user, loading } = useAuth();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [credentialsTotal, setCredentialsTotal] = useState(0);
  const { sortField, sortDir, setSort, page, setPage, pageSize, setPageSize } =
    useTableState<CredentialSortField>({
      key: "ai-providers",
      defaultSortField: "created_at",
      defaultSortDir: "desc",
      allowedSortFields: CREDENTIAL_SORT_FIELDS,
    });
  const [providerOptions, setProviderOptions] = useState<ProviderOption[]>([]);
  const [routing, setRouting] = useState<RoutingBundle | null>(null);
  const [caps, setCaps] = useState<CapsBundle | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const fetchAll = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      // settled (not all) — routing/caps endpoints may 404 on first
      // visit before the tables have any rows, and a failure there
      // shouldn't blank out the credentials table.
      const credParams = new URLSearchParams({
        sort_by: sortField,
        sort_dir: sortDir,
        limit: String(pageSize),
        offset: String((page - 1) * pageSize),
      });
      const [credsRes, optsRes, routingRes, capsRes] = await Promise.allSettled([
        apiFetch<ListEnvelope<Credential>>(
          `/api/v1/settings/ai-providers?${credParams.toString()}`
        ),
        apiFetch<ProviderOptionsResponse>(
          "/api/v1/settings/ai-providers/options"
        ),
        apiFetch<RoutingBundle>("/api/v1/settings/ai-providers/routing"),
        apiFetch<CapsBundle>("/api/v1/settings/ai-providers/caps"),
      ]);
      if (
        credsRes.status === "fulfilled" &&
        credsRes.value &&
        Array.isArray(credsRes.value.items)
      ) {
        setCredentials(credsRes.value.items);
        setCredentialsTotal(credsRes.value.total);
      } else {
        setCredentials([]);
        setCredentialsTotal(0);
      }
      if (
        optsRes.status === "fulfilled" &&
        optsRes.value &&
        Array.isArray(optsRes.value.providers)
      ) {
        setProviderOptions(optsRes.value.providers);
      }
      if (
        routingRes.status === "fulfilled" &&
        routingRes.value
      ) {
        setRouting(routingRes.value);
      }
      if (capsRes.status === "fulfilled" && capsRes.value) {
        setCaps(capsRes.value);
      }
      if (credsRes.status === "rejected") {
        setListError(
          extractErrorMessage(credsRes.reason, "Failed to load credentials")
        );
      }
    } catch (err) {
      setListError(extractErrorMessage(err, "Failed to load AI configuration"));
    } finally {
      setListLoading(false);
    }
  }, [sortField, sortDir, page, pageSize]);

  const handleSort = useCallback(
    (field: string) => {
      if (!(CREDENTIAL_SORT_FIELDS as readonly string[]).includes(field)) return;
      const f = field as CredentialSortField;
      setSort(f, f === sortField && sortDir === "asc" ? "desc" : "asc");
    },
    [sortField, sortDir, setSort],
  );

  useEffect(() => {
    if (!loading && user && isAdmin(user)) {
      fetchAll();
    }
  }, [loading, user, fetchAll]);

  const handleValidate = useCallback(
    async (id: number) => {
      setBusyId(id);
      try {
        await apiFetch(`/api/v1/settings/ai-providers/${id}/validate`, {
          method: "POST",
        });
        await fetchAll();
      } catch (err) {
        setListError(extractErrorMessage(err, "Validation failed"));
      } finally {
        setBusyId(null);
      }
    },
    [fetchAll]
  );

  const handleDelete = useCallback(
    async (id: number) => {
      if (!confirm("Delete this credential? This action cannot be undone.")) {
        return;
      }
      setBusyId(id);
      try {
        await apiFetch(`/api/v1/settings/ai-providers/${id}`, {
          method: "DELETE",
        });
        await fetchAll();
      } catch (err) {
        setListError(extractErrorMessage(err, "Delete failed"));
      } finally {
        setBusyId(null);
      }
    },
    [fetchAll]
  );

  if (loading || !user) {
    return (
      <SettingsLayout activeTab="/settings/ai-providers">
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      </SettingsLayout>
    );
  }

  if (!isAdmin(user)) {
    return (
      <SettingsLayout activeTab="/settings/ai-providers">
        <p className="text-text-muted">
          You need admin or owner role to manage AI providers.
        </p>
      </SettingsLayout>
    );
  }

  return (
    <SettingsLayout activeTab="/settings/ai-providers">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-text-primary">AI providers</h2>
          <p className="text-sm text-text-muted">
            Connect your own provider keys. Keys are encrypted at rest and never displayed after creation.
          </p>
        </div>
        <button
          type="button"
          className={btnPrimary}
          onClick={() => setShowModal(true)}
        >
          Add credential
        </button>
      </div>

      {listError && <div className={errorCls}>{listError}</div>}

      {listLoading ? (
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      ) : credentials.length === 0 ? (
        <div className={`${card} p-6 text-center text-text-muted`}>
          No credentials configured yet. Add one to enable AI features for your organization.
        </div>
      ) : (
        <div className={card}>
          <div className={cardHeader}>
            <h3 className={cardTitle}>Configured credentials</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="credentials-table">
              <thead>
                <tr className="border-b border-border text-left text-text-muted">
                  <SortableHeader
                    label="Provider"
                    field="provider"
                    activeField={sortField}
                    dir={sortDir}
                    onSort={handleSort}
                  />
                  <SortableHeader
                    label="Label"
                    field="label"
                    activeField={sortField}
                    dir={sortDir}
                    onSort={handleSort}
                  />
                  <th className="px-4 py-2">Key</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2">Models</th>
                  <th className="px-4 py-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                {credentials.map((c) => (
                  <tr key={c.id} className="border-b border-border">
                    <td className="px-4 py-2">{PROVIDER_LABELS[c.provider]}</td>
                    <td className="px-4 py-2">{c.label ?? "-"}</td>
                    <td className="px-4 py-2 font-mono text-xs">
                      {c.last_four ? `***${c.last_four}` : "-"}
                    </td>
                    <td className="px-4 py-2">
                      {c.validation_error ? (
                        <span className="text-error">{c.validation_error}</span>
                      ) : c.last_validated_at ? (
                        <span className="text-success">Valid</span>
                      ) : (
                        <span className="text-text-muted">Unknown</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs text-text-muted">
                      {c.discovered_models && c.discovered_models.length > 0
                        ? `${c.discovered_models.length} models`
                        : "-"}
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          className={btnSecondary}
                          disabled={busyId === c.id}
                          onClick={() => handleValidate(c.id)}
                        >
                          Validate now
                        </button>
                        <button
                          type="button"
                          className={btnSecondary}
                          disabled={busyId === c.id}
                          onClick={() => handleDelete(c.id)}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(credentialsTotal > pageSize || page > 1) && (
            <div className="px-4 pb-2">
              <Pagination
                page={page}
                pageSize={pageSize}
                total={credentialsTotal}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            </div>
          )}
        </div>
      )}

      {!listLoading && credentials.length > 0 && (
        <RoutingSection
          credentials={credentials}
          routing={routing}
          onSaved={fetchAll}
        />
      )}

      {!listLoading && (
        <CapsSection caps={caps} onSaved={fetchAll} />
      )}

      {showModal && (
        <AddCredentialModal
          providerOptions={providerOptions}
          onClose={() => setShowModal(false)}
          onCreated={async () => {
            setShowModal(false);
            await fetchAll();
          }}
        />
      )}
    </SettingsLayout>
  );
}


interface AddCredentialModalProps {
  providerOptions: ProviderOption[];
  onClose: () => void;
  onCreated: () => Promise<void>;
}

function AddCredentialModal({
  providerOptions,
  onClose,
  onCreated,
}: AddCredentialModalProps) {
  const [provider, setProvider] = useState<Provider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [labelText, setLabelText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const needsBaseUrl = NEEDS_BASE_URL.includes(provider);
  const allowsBearer = ALLOWS_BEARER.includes(provider);
  const apiKeyOptional = provider === "ollama";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!apiKeyOptional && !apiKey.trim()) return;
    setSubmitting(true);
    setErrorText(null);
    try {
      const body: Record<string, unknown> = { provider };
      if (apiKey.trim()) body.api_key = apiKey.trim();
      if (labelText.trim()) body.label = labelText.trim();
      if (needsBaseUrl) body.base_url = baseUrl.trim();
      if (allowsBearer && bearerToken.trim()) body.bearer_token = bearerToken.trim();
      await apiFetch("/api/v1/settings/ai-providers", {
        method: "POST",
        body: JSON.stringify(body),
      });
      await onCreated();
    } catch (err) {
      setErrorText(extractErrorMessage(err, "Failed to add credential"));
    } finally {
      setSubmitting(false);
    }
  }

  // Fall back to the static list if /options hasn't loaded — the page
  // is still functional, native just won't be visible.
  const options =
    providerOptions.length > 0
      ? providerOptions
      : (
          ["openai", "anthropic", "ollama", "openai_compatible"] as Provider[]
        ).map((k) => ({
          key: k,
          label: PROVIDER_LABELS[k],
          availability: "available" as const,
        }));

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-ai-credential-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
      >
        <h2
          id="add-ai-credential-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          Add AI credential
        </h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className={labelCls} htmlFor="ai-provider">
              Provider
            </label>
            <select
              id="ai-provider"
              className={input}
              value={provider}
              onChange={(e) => setProvider(e.target.value as Provider)}
              disabled={submitting}
            >
              {options.map((opt) => (
                <option
                  key={opt.key}
                  value={opt.key}
                  disabled={opt.availability === "not_yet_available"}
                >
                  {opt.label}
                  {opt.availability === "not_yet_available"
                    ? " (coming soon)"
                    : ""}
                </option>
              ))}
            </select>
            {provider === "native" && (
              <p className="mt-1 text-xs text-text-muted">
                Native hosted provider is coming soon (gated by
                AI_NATIVE_ENABLED).
              </p>
            )}
            {PROVIDER_DOC_LINKS[provider] && (
              <a
                href={PROVIDER_DOC_LINKS[provider]!.href}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 inline-block text-xs text-text-secondary underline hover:text-text-primary"
              >
                {PROVIDER_DOC_LINKS[provider]!.label}
              </a>
            )}
          </div>
          <div>
            <label className={labelCls} htmlFor="ai-label">
              Label (optional)
            </label>
            <input
              id="ai-label"
              type="text"
              className={input}
              value={labelText}
              onChange={(e) => setLabelText(e.target.value)}
              disabled={submitting}
              maxLength={120}
            />
          </div>
          <div>
            <label className={labelCls} htmlFor="ai-api-key">
              API key{apiKeyOptional ? " (optional)" : ""}
            </label>
            <input
              id="ai-api-key"
              type="password"
              className={input}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              disabled={submitting}
              autoComplete="off"
              required={!apiKeyOptional}
            />
            {apiKeyOptional && (
              <p className="mt-1 text-xs text-text-muted">
                Optional for self-hosted Ollama (LAN-only).
              </p>
            )}
          </div>
          {allowsBearer && (
            <div>
              <label className={labelCls} htmlFor="ai-bearer">
                Bearer token (optional)
              </label>
              <input
                id="ai-bearer"
                type="password"
                className={input}
                value={bearerToken}
                onChange={(e) => setBearerToken(e.target.value)}
                disabled={submitting}
                autoComplete="off"
              />
            </div>
          )}
          {needsBaseUrl && (
            <div>
              <label className={labelCls} htmlFor="ai-base-url">
                Base URL
              </label>
              <input
                id="ai-base-url"
                type="text"
                className={input}
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                disabled={submitting}
                placeholder="https://example.com"
                required
              />
            </div>
          )}
          {errorText && <div className={errorCls}>{errorText}</div>}
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              className={btnSecondary}
              onClick={onClose}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className={btnPrimary}
              disabled={
                submitting ||
                (!apiKeyOptional && !apiKey.trim()) ||
                (needsBaseUrl && !baseUrl.trim()) ||
                provider === "native"
              }
            >
              {submitting ? "Validating..." : "Add credential"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}


interface RoutingSectionProps {
  credentials: Credential[];
  routing: RoutingBundle | null;
  onSaved: () => Promise<void>;
}

function RoutingSection({
  credentials,
  routing,
  onSaved,
}: RoutingSectionProps) {
  const [defaultCred, setDefaultCred] = useState<number | "">(
    routing?.default?.credential_id ?? ""
  );
  const [defaultModel, setDefaultModel] = useState<string>(
    routing?.default?.model ?? ""
  );
  const [savingDefault, setSavingDefault] = useState(false);
  const [sectionError, setSectionError] = useState<string | null>(null);

  useEffect(() => {
    setDefaultCred(routing?.default?.credential_id ?? "");
    setDefaultModel(routing?.default?.model ?? "");
  }, [routing]);

  const saveDefault = async () => {
    if (!defaultCred || !defaultModel.trim()) return;
    setSavingDefault(true);
    setSectionError(null);
    try {
      await apiFetch("/api/v1/settings/ai-providers/routing/default", {
        method: "PUT",
        body: JSON.stringify({
          credential_id: defaultCred,
          model: defaultModel.trim(),
        }),
      });
      await onSaved();
    } catch (err) {
      setSectionError(extractErrorMessage(err, "Failed to save default"));
    } finally {
      setSavingDefault(false);
    }
  };

  return (
    <div className={`${card} mt-6`} data-testid="routing-section">
      <div className={cardHeader}>
        <h3 className={cardTitle}>Routing</h3>
      </div>
      <div className="p-4 space-y-6">
        {sectionError && <div className={errorCls}>{sectionError}</div>}
        <div>
          <h4 className="mb-2 text-sm font-medium text-text-primary">
            Default provider
          </h4>
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex-1 min-w-[200px]">
              <label className={labelCls} htmlFor="default-cred">
                Credential
              </label>
              <select
                id="default-cred"
                className={input}
                value={defaultCred}
                onChange={(e) =>
                  setDefaultCred(
                    e.target.value === "" ? "" : Number(e.target.value)
                  )
                }
              >
                <option value="">(choose)</option>
                {credentials.map((c) => (
                  <option key={c.id} value={c.id}>
                    {PROVIDER_LABELS[c.provider]}{c.last_four ? ` ***${c.last_four}` : ""}
                    {c.label ? ` (${c.label})` : ""}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex-1 min-w-[160px]">
              <label className={labelCls} htmlFor="default-model">
                Model
              </label>
              <input
                id="default-model"
                type="text"
                className={input}
                value={defaultModel}
                onChange={(e) => setDefaultModel(e.target.value)}
                placeholder="gpt-4o-mini"
              />
            </div>
            <button
              type="button"
              className={btnPrimary}
              onClick={saveDefault}
              disabled={
                savingDefault || !defaultCred || !defaultModel.trim()
              }
            >
              {savingDefault ? "Saving..." : "Save default"}
            </button>
          </div>
        </div>

        <FeatureRoutingTable
          credentials={credentials}
          features={routing?.features ?? []}
          onSaved={onSaved}
        />
      </div>
    </div>
  );
}


interface FeatureRoutingTableProps {
  credentials: Credential[];
  features: FeatureRouting[];
  onSaved: () => Promise<void>;
}

function FeatureRoutingTable({
  credentials,
  features,
  onSaved,
}: FeatureRoutingTableProps) {
  const [drafts, setDrafts] = useState<Record<string, { cred: number | ""; model: string }>>(
    () => {
      const m: Record<string, { cred: number | ""; model: string }> = {};
      for (const r of ROUTABLE_FEATURES) {
        const existing = features.find((f) => f.feature_name === r.key);
        m[r.key] = {
          cred: existing?.credential_id ?? "",
          model: existing?.model ?? "",
        };
      }
      return m;
    }
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const m: Record<string, { cred: number | ""; model: string }> = {};
    for (const r of ROUTABLE_FEATURES) {
      const existing = features.find((f) => f.feature_name === r.key);
      m[r.key] = {
        cred: existing?.credential_id ?? "",
        model: existing?.model ?? "",
      };
    }
    setDrafts(m);
  }, [features]);

  const save = async (featureName: string) => {
    const d = drafts[featureName];
    if (!d.cred || !d.model.trim()) return;
    setBusy(featureName);
    setErr(null);
    try {
      await apiFetch(
        `/api/v1/settings/ai-providers/routing/features/${featureName}`,
        {
          method: "PUT",
          body: JSON.stringify({
            credential_id: d.cred,
            model: d.model.trim(),
          }),
        }
      );
      await onSaved();
    } catch (e) {
      setErr(extractErrorMessage(e, "Save failed"));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (featureName: string) => {
    setBusy(featureName);
    setErr(null);
    try {
      await apiFetch(
        `/api/v1/settings/ai-providers/routing/features/${featureName}`,
        { method: "DELETE" }
      );
      await onSaved();
    } catch (e) {
      setErr(extractErrorMessage(e, "Remove failed"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <h4 className="mb-2 text-sm font-medium text-text-primary">
        Per-feature overrides
      </h4>
      <p className="mb-2 text-xs text-text-muted">
        Falls back to the default when an override is removed. PR1 stores
        these; dispatch wires up in a later PR.
      </p>
      {err && <div className={errorCls}>{err}</div>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="feature-routing-table">
          <thead>
            <tr className="border-b border-border text-left text-text-muted">
              <th className="px-2 py-1">Feature</th>
              <th className="px-2 py-1">Credential</th>
              <th className="px-2 py-1">Model</th>
              <th className="px-2 py-1">Actions</th>
            </tr>
          </thead>
          <tbody>
            {ROUTABLE_FEATURES.map((f) => {
              const draft = drafts[f.key];
              const hasOverride = features.some(
                (x) => x.feature_name === f.key
              );
              return (
                <tr key={f.key} className="border-b border-border">
                  <td className="px-2 py-2">{f.label}</td>
                  <td className="px-2 py-2">
                    <select
                      className={input}
                      value={draft.cred}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [f.key]: {
                            ...prev[f.key],
                            cred:
                              e.target.value === ""
                                ? ""
                                : Number(e.target.value),
                          },
                        }))
                      }
                    >
                      <option value="">(use default)</option>
                      {credentials.map((c) => (
                        <option key={c.id} value={c.id}>
                          {PROVIDER_LABELS[c.provider]}{c.last_four ? ` ***${c.last_four}` : ""}
                          {c.label ? ` (${c.label})` : ""}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-2 py-2">
                    <input
                      type="text"
                      className={input}
                      value={draft.model}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [f.key]: { ...prev[f.key], model: e.target.value },
                        }))
                      }
                      placeholder="model name"
                    />
                  </td>
                  <td className="px-2 py-2">
                    <div className="flex gap-1">
                      <button
                        type="button"
                        className={btnSecondary}
                        disabled={
                          busy === f.key ||
                          !draft.cred ||
                          !draft.model.trim()
                        }
                        onClick={() => save(f.key)}
                      >
                        Save
                      </button>
                      {hasOverride && (
                        <button
                          type="button"
                          className={btnSecondary}
                          disabled={busy === f.key}
                          onClick={() => remove(f.key)}
                        >
                          Remove
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}


interface CapsSectionProps {
  caps: CapsBundle | null;
  onSaved: () => Promise<void>;
}

function CapsSection({ caps, onSaved }: CapsSectionProps) {
  const [defaultSoft, setDefaultSoft] = useState<string>(
    caps?.default?.soft_cap_cents != null
      ? (caps.default.soft_cap_cents / 100).toString()
      : ""
  );
  const [defaultHard, setDefaultHard] = useState<string>(
    caps?.default?.hard_cap_cents != null
      ? (caps.default.hard_cap_cents / 100).toString()
      : ""
  );
  const [saving, setSaving] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  useEffect(() => {
    setDefaultSoft(
      caps?.default?.soft_cap_cents != null
        ? (caps.default.soft_cap_cents / 100).toString()
        : ""
    );
    setDefaultHard(
      caps?.default?.hard_cap_cents != null
        ? (caps.default.hard_cap_cents / 100).toString()
        : ""
    );
  }, [caps]);

  const parseDollars = (v: string): number | null => {
    if (v.trim() === "") return null;
    const n = Number(v);
    if (!Number.isFinite(n) || n < 0) return null;
    return Math.round(n * 100);
  };

  const saveDefault = async () => {
    const soft = parseDollars(defaultSoft);
    const hard = parseDollars(defaultHard);
    if (
      soft !== null &&
      hard !== null &&
      hard < soft
    ) {
      setErrorText("Hard cap must be greater than or equal to soft cap.");
      return;
    }
    setSaving(true);
    setErrorText(null);
    try {
      await apiFetch("/api/v1/settings/ai-providers/caps/default", {
        method: "PUT",
        body: JSON.stringify({
          soft_cap_cents: soft,
          hard_cap_cents: hard,
          period: "monthly",
        }),
      });
      await onSaved();
    } catch (e) {
      setErrorText(extractErrorMessage(e, "Failed to save caps"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={`${card} mt-6`} data-testid="caps-section">
      <div className={cardHeader}>
        <h3 className={cardTitle}>Monthly spend caps</h3>
      </div>
      <div className="p-4 space-y-4">
        <p className="text-xs text-text-muted">
          Caps are stored now and enforced when dispatch ships in a later
          PR. Soft cap fires a notification; hard cap refuses new calls.
          Leave blank for no cap on that axis.
        </p>
        {errorText && <div className={errorCls}>{errorText}</div>}
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label className={labelCls} htmlFor="default-soft">
              Default soft cap (USD)
            </label>
            <input
              id="default-soft"
              type="number"
              step="0.01"
              min="0"
              className={input}
              value={defaultSoft}
              onChange={(e) => setDefaultSoft(e.target.value)}
              placeholder="e.g. 25.00"
            />
          </div>
          <div>
            <label className={labelCls} htmlFor="default-hard">
              Default hard cap (USD)
            </label>
            <input
              id="default-hard"
              type="number"
              step="0.01"
              min="0"
              className={input}
              value={defaultHard}
              onChange={(e) => setDefaultHard(e.target.value)}
              placeholder="e.g. 50.00"
            />
          </div>
          <button
            type="button"
            className={btnPrimary}
            onClick={saveDefault}
            disabled={saving}
          >
            {saving ? "Saving..." : "Save caps"}
          </button>
        </div>
      </div>
    </div>
  );
}
