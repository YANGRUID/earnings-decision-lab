import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { ErrorState, LoadingState } from "../components/StatusStates";
import { PreparationResult } from "../components/PreparationResult";
import { OverviewTab } from "../components/workspace/OverviewTab";
import { UpcomingEarningsTab } from "../components/workspace/UpcomingEarningsTab";
import { StrategyLabTab } from "../components/workspace/StrategyLabTab";
import { ThesisTab } from "../components/workspace/ThesisTab";
import { DecisionTab } from "../components/workspace/DecisionTab";
import { ExposureTab } from "../components/workspace/ExposureTab";
import { HistoricalEventsTab } from "../components/workspace/HistoricalEventsTab";
import type { ResearchJob, ResearchOverview } from "../types/api";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "upcoming", label: "Upcoming Earnings" },
  { key: "strategy", label: "Strategy Lab" },
  { key: "thesis", label: "AI Thesis" },
  { key: "decision", label: "AI Decision" },
  { key: "exposure", label: "My Exposure" },
  { key: "historical", label: "Historical Events" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const POLL_INTERVAL_MS = 2000;

function isJobRunning(job: ResearchJob | null): boolean {
  return job?.status === "running";
}

export function CompanyWorkspace() {
  const { ticker: rawTicker = "" } = useParams();
  const ticker = rawTicker.toUpperCase();
  const [tab, setTab] = useState<TabKey>("overview");
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [prepareError, setPrepareError] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [lastAction, setLastAction] = useState<"prepare" | "refresh" | null>(null);
  // True once *this session's* polling has actually watched a job move
  // through running -> terminal -- as opposed to a job we only ever saw
  // already-finished (e.g. straight from the initial overview fetch on
  // page load). Controls whether the result card opens expanded by
  // default: something you just triggered should be visible immediately;
  // history from a previous visit shouldn't force itself open every time.
  const [justFinished, setJustFinished] = useState(false);
  // Bumped whenever a preparation run finishes, to force a fresh overview
  // fetch -- useAsync only refetches when one of its deps changes.
  const [refreshKey, setRefreshKey] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const overview = useAsync(() => api.getResearchOverview(ticker), [ticker, refreshKey]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const status = await api.getResearchStatus(ticker);
        setJob(status);
        if (status.status !== "running") {
          stopPolling();
          setJustFinished(true);
          setRefreshKey((k) => k + 1);
        }
      } catch {
        stopPolling();
      }
    }, POLL_INTERVAL_MS);
  }, [ticker, stopPolling]);

  useEffect(() => {
    if (overview.data?.latest_job && isJobRunning(overview.data.latest_job)) {
      setJob(overview.data.latest_job);
      startPolling();
    }
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview.data?.ticker]);

  const triggerPrepare = async (kind: "prepare" | "refresh") => {
    setPreparing(true);
    setPrepareError(null);
    setLastAction(kind);
    try {
      const result = kind === "prepare" ? await api.prepareResearch(ticker) : await api.refreshResearch(ticker);
      if ("id" in result) {
        setJob(result);
        if (isJobRunning(result)) startPolling();
      } else {
        // queued -- the real job row appears within moments; start polling
        // for it immediately rather than waiting for a fixed delay.
        startPolling();
      }
    } catch (err) {
      setPrepareError(err instanceof ApiError ? err.message : "Could not start preparation.");
    } finally {
      setPreparing(false);
    }
  };

  // Only the very first load (nothing to show yet) blocks the whole page --
  // a background refetch triggered by Refresh/Retry must never blank a
  // workspace that already has valid data on screen (see Phase 14.9 Part
  // B: "do NOT blank the current UI while refresh runs").
  if (overview.loading && !overview.data) return <LoadingState label={`Loading ${ticker}…`} />;
  if (overview.error && !overview.data) return <ErrorState message={overview.error} />;
  if (!overview.data) return null;

  const data: ResearchOverview = overview.data;
  const jobRunning = isJobRunning(job) || isJobRunning(data.latest_job);
  const activeJob = job ?? data.latest_job;

  if (!data.company) {
    return (
      <div>
        <div className="page-header">
          <h1>{ticker}</h1>
          <p>Not researched yet.</p>
        </div>
        <div className="card">
          <h2>Prepare research for {ticker}</h2>
          <p className="text-sm text-muted">
            Pulls real historical earnings, price history, SEC filings, analyst estimates, and
            (where available) real options-chain data for {ticker}. Nothing here is fabricated —
            each step either succeeds with real data or is honestly marked unavailable.
          </p>
          {!jobRunning && (
            <button className="btn" onClick={() => triggerPrepare("prepare")} disabled={preparing}>
              {preparing ? "Starting…" : `Prepare ${ticker}`}
            </button>
          )}
          {prepareError && <div className="notice" style={{ marginTop: 12 }}>{prepareError}</div>}
        </div>
        {activeJob && (
          <PreparationResult
            job={activeJob}
            justFinished={justFinished}
            onRetryMissingData={() => triggerPrepare("prepare")}
            retrying={preparing}
          />
        )}
      </div>
    );
  }

  // A refetch (of an *existing* workspace) failing must never discard data
  // already on screen -- surface it as a dismissable-by-retry banner and
  // keep rendering the last known-good `data` underneath.
  const refetchFailedWithStaleData = overview.error !== null;

  return (
    <div>
      <div className="page-header workspace-header">
        <div>
          <h1>{ticker}</h1>
          <p>{data.company.name}</p>
        </div>
        <button
          className="btn-secondary"
          onClick={() => triggerPrepare("refresh")}
          disabled={preparing || jobRunning}
        >
          {jobRunning ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {prepareError && (
        <div className="notice">
          {lastAction === "refresh"
            ? `Refresh failed — displaying last successful data. (${prepareError})`
            : prepareError}
        </div>
      )}
      {refetchFailedWithStaleData && (
        <div className="notice">
          Refresh failed — displaying last successful data. ({overview.error})
        </div>
      )}

      {activeJob && (
        <PreparationResult
          job={activeJob}
          justFinished={justFinished}
          onRetryMissingData={() => triggerPrepare("prepare")}
          retrying={preparing}
        />
      )}

      <div className="tab-bar">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab-button ${tab === t.key ? "active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="tab-panel">
        {tab === "overview" && <OverviewTab overview={data} />}
        {tab === "upcoming" && (
          <UpcomingEarningsTab
            ticker={ticker}
            overview={data}
            onOverviewChanged={() => setRefreshKey((k) => k + 1)}
          />
        )}
        {tab === "strategy" && <StrategyLabTab ticker={ticker} />}
        {tab === "thesis" && <ThesisTab ticker={ticker} />}
        {tab === "decision" && <DecisionTab ticker={ticker} />}
        {tab === "exposure" && <ExposureTab ticker={ticker} />}
        {tab === "historical" && <HistoricalEventsTab ticker={ticker} />}
      </div>
    </div>
  );
}
