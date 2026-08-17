export function formatMoney(value: string | null, digits = 2): string {
  if (value === null) return "—";
  const num = Number(value);
  return num.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}
