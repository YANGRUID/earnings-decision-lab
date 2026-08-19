import { useState } from "react";
import { api, ApiError } from "../../api/client";

/** Add/Replace/Remove key for a single real provider adapter -- the raw
 * key is only ever typed into this form and sent once, over PUT; it is
 * never rendered back (the dashboard only ever shows a masked suffix, see
 * ProviderStatus.masked_key) and this component never stores it in any
 * persisted client-side state (no localStorage, nothing outside this
 * form's own transient input value). See services/secret_store/ on the
 * backend for how it's actually stored. */
export function ProviderCredentialForm({
  provider,
  configured,
  needsBaseUrl,
  onChanged,
}: {
  provider: string;
  configured: boolean;
  needsBaseUrl?: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await api.setProviderCredential(provider, {
        api_key: apiKey,
        base_url: needsBaseUrl ? baseUrl : undefined,
        model: needsBaseUrl ? model || undefined : undefined,
      });
      setApiKey("");
      setNotice(configured ? "Key replaced." : "Key saved.");
      setOpen(false);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save credential.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await api.deleteProviderCredential(provider);
      setNotice("Key removed.");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove credential.");
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn-secondary" onClick={() => setOpen(true)} disabled={saving}>
          {configured ? "Replace key" : "Add key"}
        </button>
        {configured && (
          <button className="btn-secondary" onClick={remove} disabled={saving}>
            Remove key
          </button>
        )}
        {notice && <span className="text-sm text-muted">{notice}</span>}
        {error && <span className="text-sm" style={{ color: "var(--color-negative)" }}>{error}</span>}
      </div>
    );
  }

  return (
    <div style={{ marginTop: 8 }}>
      <div className="grid grid-2" style={{ gap: 10 }}>
        <div className="field">
          <label>API key</label>
          <input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="Paste the real key -- never shown again after saving"
          />
        </div>
        {needsBaseUrl && (
          <div className="field">
            <label>Base URL</label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-endpoint.example.com/v1"
            />
          </div>
        )}
        {needsBaseUrl && (
          <div className="field">
            <label>Model (optional)</label>
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button
          className="btn"
          onClick={save}
          disabled={saving || !apiKey.trim() || (needsBaseUrl && !baseUrl.trim())}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="btn-secondary" onClick={() => setOpen(false)} disabled={saving}>
          Cancel
        </button>
      </div>
      {error && (
        <p className="text-sm" style={{ color: "var(--color-negative)", marginBottom: 0 }}>
          {error}
        </p>
      )}
    </div>
  );
}
