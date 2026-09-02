// V4 consolidation -- shared constants and pure formatting helpers for the
// V4 surfaces. Components live in sharedComponents.tsx (react-refresh rule).

export const CONFIG_ORDER = [
  "v4_2k_conservative",
  "v4_2k_moderate",
  "v4_2k_aggressive",
  "v4_10k_conservative",
  "v4_10k_moderate",
  "v4_10k_aggressive",
] as const;

export function configLabel(key: string): string {
  const [, cap, risk] = key.split("_");
  const capital = cap === "2k" ? "$2,000" : cap === "10k" ? "$10,000" : cap;
  return `${capital} ${risk ? risk[0].toUpperCase() + risk.slice(1) : ""}`.trim();
}

export function shortConfigLabel(key: string): string {
  const [, cap, risk] = key.split("_");
  return `$${cap?.toUpperCase()} ${risk ? risk[0].toUpperCase() : ""}`;
}

export function pct(value: string | null | undefined, digits = 1): string {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function money(value: string | number | null | undefined, digits = 0): string {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function humanStrategy(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Section 50 colour semantics. Green = healthy/observed, red = system
// failure, amber = warning/incomplete/delayed, blue = experimental, gray
// = n/a. A NO_ACTION is not a failure and a losing trade is not an error.
export function statusPill(status: string | null | undefined): {
  className: string;
  label: string;
} {
  switch (status) {
    case "RANKED":
    case "OBSERVED":
    case "SETTLED":
    case "CAPTURED":
      return { className: "pill pill-positive", label: humanStatus(status) };
    case "NO_ACTION":
      return { className: "pill pill-neutral", label: "No action" };
    case "FAILED":
    case "OBSERVATION_FAILED":
    case "NOT_EXECUTABLE":
      return { className: "pill pill-negative", label: humanStatus(status) };
    case "PENDING":
    case "WAITING":
      return { className: "pill pill-warning", label: humanStatus(status) };
    default:
      return { className: "pill pill-neutral", label: humanStatus(status) };
  }
}

// Section 51 -- user-facing language, never raw enum strings as the primary
// label. The raw value remains available in Advanced/tooltips.
export function humanStatus(status: string | null | undefined): string {
  switch (status) {
    case "RANKED":
      return "Recommended";
    case "NO_ACTION":
      return "No action";
    case "FAILED":
      return "Failed";
    case "OBSERVED":
      return "Entry observed";
    case "NOT_EXECUTABLE":
      return "Required quote unavailable";
    case "SETTLED":
      return "Settled";
    case "OBSERVATION_FAILED":
      return "Settlement observation error";
    case "PENDING_ENTRY":
      return "Waiting for entry observation";
    case "CAPTURED":
      return "Entry captured";
    default:
      return status ?? "—";
  }
}

export function humanReasonCode(code: string): string {
  switch (code) {
    case "SETTLEMENT_WINDOW_MISSED":
      return "Settlement window missed: the legal T+1 exit window (15:55 ET, ±5 min) passed without an observation, so this position was closed unsettled rather than priced from a later quote";
    case "STRATEGY_FAMILY_NOT_ALLOWED":
      return "Strategy family not allowed for this risk profile";
    case "CAPITAL_INSUFFICIENT":
      return "Capital insufficient";
    case "RISK_CAP_EXCEEDED":
      return "Risk cap exceeded";
    case "UNDEFINED_RISK_NOT_SIZEABLE":
      return "Unbounded risk — not sizeable";
    case "NOT_PRICEABLE":
      return "Required quote unavailable";
    case "RESEARCH_NOT_READY":
      return "Research not ready";
    case "CONTRACT_RESOLUTION_FAILED":
      return "Contract resolution failed";
    default:
      return code.replace(/_/g, " ").toLowerCase();
  }
}


// Section 21 -- consistent numeric presentation. Never render raw
// "347.500000" strings; format at the boundary.
export function fmtPrice(value: string | number | null | undefined, digits = 2): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  return Number.isNaN(n) ? "—" : n.toFixed(digits);
}
export function fmtStrike(value: string | number | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  return Number.isInteger(n) ? n.toFixed(0) : n.toFixed(2).replace(/\.?0+$/, "");
}
export function fmtGreek(value: string | number | null | undefined, digits = 3): string {
  if (value == null) return "—";
  const n = Number(value);
  return Number.isNaN(n) ? "—" : n.toFixed(digits);
}
export function fmtIv(value: string | number | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  return Number.isNaN(n) ? "—" : `${(n * 100).toFixed(1)}%`;
}
export function fmtDte(expiration: string | null | undefined, from: Date = new Date()): string {
  if (!expiration) return "—";
  const d = Math.round((new Date(expiration + "T21:00:00Z").getTime() - from.getTime()) / 86400000);
  return `${d} DTE`;
}

export function fmtMarketCap(value: string | number | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  return `$${n.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}
