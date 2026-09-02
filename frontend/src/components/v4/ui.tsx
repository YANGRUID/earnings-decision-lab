import type { ReactNode } from "react";
import { humanReasonCode, humanStatus, statusPill } from "./shared";

// V4 consolidation, Section 18 -- shared presentational primitives. Every
// V4 surface composes these instead of re-implementing pills, metrics,
// section headers, notices and provenance badges.

export function StatusPill({ status, title }: { status: string | null | undefined; title?: string }) {
  const p = statusPill(status);
  return <span className={p.className} title={title ?? status ?? undefined}>{p.label}</span>;
}

export function LifecyclePill({ lifecycle }: { lifecycle: string | null | undefined }) {
  const map: Record<string, { cls: string; label: string }> = {
    RANKED: { cls: "pill pill-positive", label: "Recommended" },
    NO_ACTION: { cls: "pill pill-neutral", label: "No action" },
    FAILED: { cls: "pill pill-negative", label: "Failed" },
    WAITING_ENTRY: { cls: "pill pill-warning", label: "Waiting for entry observation" },
    ENTRY_OBSERVED: { cls: "pill pill-positive", label: "Entry observed" },
    ENTRY_FAILED: { cls: "pill pill-negative", label: "Entry observation failed" },
    WAITING_SETTLEMENT: { cls: "pill pill-warning", label: "Waiting for post-earnings settlement" },
    SETTLED: { cls: "pill pill-positive", label: "Settled" },
    SETTLEMENT_FAILED: { cls: "pill pill-negative", label: "Settlement observation error" },
  };
  const v = (lifecycle && map[lifecycle]) || { cls: "pill pill-neutral", label: humanStatus(lifecycle) };
  return <span className={v.cls} data-lifecycle={lifecycle ?? ""}>{v.label}</span>;
}

export function Metric({ label, value, sub, mono = false, testId }: {
  label: string; value: ReactNode; sub?: ReactNode; mono?: boolean; testId?: string;
}) {
  return (
    <div className="stat" data-testid={testId}>
      <span className="stat-label">{label}</span>
      <span className={`stat-value small${mono ? " mono" : ""}`}>{value ?? "—"}</span>
      {sub ? <span className="text-faint text-sm">{sub}</span> : null}
    </div>
  );
}

export function SectionHeader({ title, eyebrow, right }: { title: string; eyebrow?: string; right?: ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, marginBottom: 8 }}>
      <div>
        {eyebrow && <div className="stat-label">{eyebrow}</div>}
        <h2 style={{ margin: 0 }}>{title}</h2>
      </div>
      {right}
    </div>
  );
}

export function Notice({ kind = "info", children, testId }: { kind?: "info" | "warn" | "critical" | "success"; children: ReactNode; testId?: string }) {
  const cls = kind === "critical" ? "notice notice-critical" : kind === "success" ? "notice notice-success" : kind === "warn" ? "notice" : "notice";
  return <div className={cls} data-testid={testId} role="note">{children}</div>;
}

export function MarketDataQualityBadge({ quality, provider, connected = true, staleLabel }: { quality: string | null | undefined; provider?: string | null; connected?: boolean; staleLabel?: string }) {
  const q = quality?.toLowerCase();
  const prov = provider ? (provider === "ibkr_tws" || provider === "tws" ? "TWS" : provider.toUpperCase()) : null;
  if (!q) {
    return <span className="pill pill-neutral" data-testid="md-quality" title={staleLabel ?? "No quote observed since the last restart"}>{prov ? `${prov} · ` : ""}{staleLabel ?? (connected ? "awaiting first market-data observation" : "no market data")}</span>;
  }
  const cls = q === "live" ? "pill pill-positive" : q === "delayed" ? "pill pill-warning" : "pill pill-neutral";
  return <span className={cls} data-testid="md-quality">{prov ? `${prov} · ` : ""}{q.toUpperCase()}</span>;
}

export function ProvenanceBadge({ provider, model, version }: { provider?: string | null; model?: string | null; version?: string | null }) {
  const parts = [provider, model, version].filter(Boolean);
  if (parts.length === 0) return null;
  return <span className="pill pill-neutral mono" style={{ fontSize: ".7rem" }}>{parts.join(" · ")}</span>;
}

export function Timestamp({ iso, tz = "America/New_York", withDate = true }: { iso: string | null | undefined; tz?: string; withDate?: boolean }) {
  if (!iso) return <span className="text-faint">—</span>;
  const d = new Date(iso);
  const text = withDate
    ? d.toLocaleString("en-US", { timeZone: tz, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
  return <time className="mono" dateTime={iso} title={d.toISOString()}>{text} {tz === "America/New_York" ? "ET" : ""}</time>;
}

export function FailureExplanation({ category, detail, requiredSide, provider, quality, retryable }: {
  category: string | null | undefined; detail?: string | null; requiredSide?: string | null;
  provider?: string | null; quality?: string | null; retryable?: boolean | null;
}) {
  return (
    <div className="card" data-testid="failure-explanation" style={{ borderLeft: "4px solid var(--fail, #95322e)" }}>
      <SectionHeader title={category ? humanReasonCode(category) : "Observation failed"} eyebrow="Why this did not observe" />
      {detail && <p className="text-muted" style={{ margin: "0 0 8px" }}>{detail}</p>}
      <div className="grid grid-4" style={{ gap: 8 }}>
        {requiredSide && <Metric label="Required quote side" value={requiredSide.toUpperCase()} mono />}
        {provider && <Metric label="Provider" value={provider === "ibkr_tws" ? "TWS" : provider} mono />}
        <Metric label="Market data" value={<MarketDataQualityBadge quality={quality} />} />
        {retryable != null && <Metric label="Retry" value={retryable ? "Will retry at the next window" : "Not retryable"} />}
      </div>
      <p className="text-faint text-sm" style={{ margin: "8px 0 0" }}>A failed observation is a data-access outcome, not a losing trade.</p>
    </div>
  );
}

export function ConfigurationSelector({ selected, available, onSelect }: { selected: string; available: string[]; onSelect: (k: string) => void }) {
  const [, cap, risk] = selected.split("_");
  const has = (k: string) => available.includes(k);
  const pick = (c: string, r: string) => onSelect(`v4_${c}_${r}`);
  return (
    <div className="card" data-testid="config-selector">
      <SectionHeader title="Configuration" />
      <p className="text-muted text-sm" style={{ marginTop: 0 }}>
        Switch between the six frozen results. No model call, no market-data request — every
        configuration was evaluated once, on the same evidence, when this decision was frozen.
      </p>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        <div>
          <div className="stat-label">Capital</div>
          <div className="tab-bar">
            {["2k", "10k"].map((c) => (
              <button key={c} className={`tab-button ${cap === c ? "active" : ""}`} disabled={!has(`v4_${c}_${risk}`)} onClick={() => pick(c, risk)}>
                {c === "2k" ? "$2,000" : "$10,000"}
              </button>
            ))}
          </div>
        </div>
        <div>
          <div className="stat-label">Risk profile</div>
          <div className="tab-bar">
            {["conservative", "moderate", "aggressive"].map((r) => (
              <button key={r} className={`tab-button ${risk === r ? "active" : ""}`} disabled={!has(`v4_${cap}_${r}`)} onClick={() => pick(cap, r)}>
                {r[0].toUpperCase() + r.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
