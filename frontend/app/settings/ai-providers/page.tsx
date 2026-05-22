"use client";

import { useCallback, useEffect, useState } from "react";
import SettingsLayout from "@/components/SettingsLayout";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
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

type Provider = "openai" | "anthropic" | "ollama" | "openai_compatible";

interface Credential {
  id: number;
  org_id: number;
  provider: Provider;
  last_four: string;
  key_fingerprint: string;
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

const PROVIDER_LABELS: Record<Provider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  ollama: "Ollama",
  openai_compatible: "OpenAI-compatible",
};

const NEEDS_BASE_URL: Provider[] = ["ollama", "openai_compatible"];
const ALLOWS_BEARER: Provider[] = ["ollama"];


export default function AiProvidersPage() {
  const { user, loading } = useAuth();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const fetchList = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const data = await apiFetch<Credential[]>("/api/v1/settings/ai-providers");
      setCredentials(data);
    } catch (err) {
      setListError(extractErrorMessage(err, "Failed to load credentials"));
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!loading && user && isAdmin(user)) {
      fetchList();
    }
  }, [loading, user, fetchList]);

  const handleValidate = useCallback(
    async (id: number) => {
      setBusyId(id);
      try {
        await apiFetch(`/api/v1/settings/ai-providers/${id}/validate`, {
          method: "POST",
        });
        await fetchList();
      } catch (err) {
        setListError(extractErrorMessage(err, "Validation failed"));
      } finally {
        setBusyId(null);
      }
    },
    [fetchList]
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
        await fetchList();
      } catch (err) {
        setListError(extractErrorMessage(err, "Delete failed"));
      } finally {
        setBusyId(null);
      }
    },
    [fetchList]
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
                  <th className="px-4 py-2">Provider</th>
                  <th className="px-4 py-2">Label</th>
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
                      ***{c.last_four}
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
        </div>
      )}

      {showModal && (
        <AddCredentialModal
          onClose={() => setShowModal(false)}
          onCreated={async () => {
            setShowModal(false);
            await fetchList();
          }}
        />
      )}
    </SettingsLayout>
  );
}


interface AddCredentialModalProps {
  onClose: () => void;
  onCreated: () => Promise<void>;
}

function AddCredentialModal({ onClose, onCreated }: AddCredentialModalProps) {
  const [provider, setProvider] = useState<Provider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [labelText, setLabelText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const needsBaseUrl = NEEDS_BASE_URL.includes(provider);
  const allowsBearer = ALLOWS_BEARER.includes(provider);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!apiKey.trim()) return;
    setSubmitting(true);
    setErrorText(null);
    try {
      const body: Record<string, unknown> = {
        provider,
        api_key: apiKey,
      };
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
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="ollama">Ollama</option>
              <option value="openai_compatible">OpenAI-compatible</option>
            </select>
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
              API key
            </label>
            <input
              id="ai-api-key"
              type="password"
              className={input}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              disabled={submitting}
              autoComplete="off"
              required
            />
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
              disabled={submitting || !apiKey.trim() || (needsBaseUrl && !baseUrl.trim())}
            >
              {submitting ? "Validating..." : "Add credential"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
