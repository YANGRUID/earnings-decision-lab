/** True when `value` carries real descriptive information worth showing a
 * field for -- false for null/empty/"unknown"/"n/a" style placeholders.
 * Use to omit a whole metadata row rather than rendering "Sector: Unknown"
 * -- meaningless descriptive metadata should be hidden entirely, unlike an
 * unknown *research* state (e.g. next earnings date), which must still be
 * shown with an explanation. See docs/limitations.md's UX principle. */
export function isMeaningfulValue(value: string | null | undefined): value is string {
  if (value === null || value === undefined) return false;
  const trimmed = value.trim();
  if (trimmed === "") return false;
  const lowered = trimmed.toLowerCase();
  return lowered !== "unknown" && lowered !== "n/a" && lowered !== "none";
}

export function formatMoney(value: string | null, digits = 2): string {
  if (value === null) return "—";
  const num = Number(value);
  return num.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPercent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatPlainPercent(value: string | null, digits = 1): string {
  if (value === null) return "—";
  return formatPercent(Number(value), digits);
}

export const PROVIDER_LABELS: Record<string, string> = {
  tiingo: "Tiingo",
  alpha_vantage: "Alpha Vantage",
  earningsapi: "EarningsAPI",
  finnhub: "Finnhub",
  sec_edgar: "SEC EDGAR",
  ibkr: "Interactive Brokers",
  deepseek: "DeepSeek",
  openai: "OpenAI",
  anthropic: "Anthropic",
  openai_compatible: "OpenAI-compatible",
};

export function providerLabel(provider: string | null): string {
  if (provider === null) return "—";
  return PROVIDER_LABELS[provider] ?? provider;
}

/** Providers with a real credential form -- mirrors the backend's
 * services.secret_store.CREDENTIAL_PROVIDERS exactly. SEC EDGAR (no key
 * concept) and IBKR (local Gateway session, never a stored key)
 * deliberately have no entry here. */
export const CREDENTIAL_PROVIDERS = [
  "tiingo",
  "alpha_vantage",
  "deepseek",
  "openai",
  "anthropic",
  "openai_compatible",
] as const;

export const DOMAIN_LABELS: Record<string, string> = {
  price_history: "Market Price Data",
  earnings_estimates: "Earnings Estimates",
  earnings_calendar: "Earnings Calendar",
  filings: "SEC Filings",
  options: "Options Data",
  llm: "AI / LLM",
};

export const DATA_STATE_LABELS: Record<string, string> = {
  live: "Live",
  delayed: "Delayed",
  frozen: "Frozen",
  stale: "Stale",
  previous_session: "Previous session",
  market_closed: "Market closed",
  gateway_disconnected: "Gateway disconnected",
  provider_unavailable: "Provider unavailable",
  rate_limited: "Rate limited",
  premium_required: "Premium required",
  not_collected: "Not collected",
  unsupported: "Unsupported",
  unknown: "Unknown",
};

export function dataStateLabel(state: string): string {
  return DATA_STATE_LABELS[state] ?? state;
}

export const MARKET_SESSION_LABELS: Record<string, string> = {
  pre_market: "Pre-market",
  regular: "Market open",
  after_hours: "After-hours",
  closed: "Market closed",
};

export function formatRelativeTime(iso: string | null): string {
  if (iso === null) return "never";
  const then = new Date(iso).getTime();
  const now = Date.now();
  const minutes = Math.max(0, Math.round((now - then) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
