export function HistoricalReplay() {
  return (
    <div>
      <div className="page-header">
        <h1>Historical Replay</h1>
        <p>Rule-based reconstruction of options strategies entered before past earnings events.</p>
      </div>

      <div className="card">
        <h2>No historical option-chain data available yet</h2>
        <p className="text-sm text-muted">
          The replay engine (deterministic strike selection, entry economics, payoff evaluation)
          is implemented and unit-tested — see{" "}
          <a
            href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/earnings_methodology.md"
            target="_blank"
            rel="noreferrer"
          >
            docs/earnings_methodology.md
          </a>
          . No historical options-chain provider is configured yet (every free option evaluated
          either lacks historical coverage or requires a paid subscription — see{" "}
          <a
            href="https://github.com/YANGRUID/earnings-decision-lab/blob/main/docs/data_sources.md"
            target="_blank"
            rel="noreferrer"
          >
            docs/data_sources.md
          </a>
          ), so there is nothing real to reconstruct a strategy from yet. This screen will
          populate automatically once one is wired up — no code change needed here.
        </p>
      </div>
    </div>
  );
}
