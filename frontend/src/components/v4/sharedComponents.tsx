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

export function ExperimentalNotice({ text }: { text?: string }) {
  return (
    <div className="notice" role="note">
      <strong>Experimental forward test.</strong>{" "}
      {text ??
        "V4 shadow evidence is prospective, immutable and never used to place orders. It is not the official V3 control cohort."}
    </div>
  );
}
