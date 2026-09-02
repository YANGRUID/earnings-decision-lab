// V4 consolidation -- shared presentational components.

export function MethodologyDetails({ versions }: { versions: Record<string, string | null> | null | undefined }) {
  if (!versions) return null;
  return (
    <details style={{ marginTop: 10 }}>
      <summary className="text-muted text-sm">Methodology &amp; provenance (advanced)</summary>
      <table style={{ marginTop: 8, fontSize: ".8rem" }}>
        <tbody>
          {Object.entries(versions).map(([k, v]) => (
            <tr key={k}>
              <td className="text-muted" style={{ paddingRight: 12 }}>{k}</td>
              <td className="mono">{v ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export function ForwardTestNotice({ text }: { text?: string }) {
  return (
    <div className="notice" role="note">
      <strong>V4 forward test.</strong>{" "}
      {text ??
        "Evidence is prospective and immutable: decided at 15:30 ET, settled at 15:30 ET on the first post-earnings trading day, never used to place orders."}
    </div>
  );
}
