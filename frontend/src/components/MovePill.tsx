/** Renders a percentage-move value with the standard finance-UI convention
 * (green for positive, red for negative) — not a gimmick, an expected
 * reading aid for this content type. */
export function MovePill({ value }: { value: string | null }) {
  if (value === null) return <span className="text-faint">—</span>;
  const pct = Number(value) * 100;
  const sign = pct >= 0 ? "+" : "";
  return (
    <span className={`pill ${pct >= 0 ? "pill-positive" : "pill-negative"}`}>
      {sign}
      {pct.toFixed(2)}%
    </span>
  );
}
