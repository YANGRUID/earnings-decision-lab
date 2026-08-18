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

export const DOMAIN_LABELS: Record<string, string> = {
  price_history: "Market Price Data",
  earnings_estimates: "Earnings Estimates",
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
