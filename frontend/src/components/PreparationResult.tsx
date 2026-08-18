import { useEffect, useRef, useState } from "react";
import { PreparationProgress } from "./PreparationProgress";
import type { ResearchJob } from "../types/api";

const HEADLINE: Record<ResearchJob["status"], string> = {
  running: "Research preparation running",
  completed: "Research preparation completed",
  completed_with_warnings: "Research preparation completed with warnings",
  failed: "Research preparation failed",
};

/** Whether re-running "Prepare" (non-forced) could plausibly fix at least
 * one step -- i.e. there's a failed/skipped-with-a-real-reason step that
 * isn't explicitly marked non-retryable (e.g. today's Alpha Vantage quota
 * already spent). Steps that are `skipped` because there's simply no
 * provider configured for that data class aren't included: re-running
 * won't change that without a settings change first. */
function hasRetryableGap(job: ResearchJob): boolean {
  return job.steps.some((s) => s.status === "failed" && s.retryable !== false);
}

/** The persistent result of the most recent preparation/refresh run --
 * unlike the old behavior, this never disappears the instant the job
 * finishes. It stays on screen (collapsed by default once it's from a
 * page load rather than something the user just triggered) so "what
 * succeeded, what failed, what's still usable, what can be retried" is
 * always answerable, not just visible for the few seconds a job is
 * running (see Phase 14.9 Part B). */
export function PreparationResult({
  job,
  justFinished,
  onRetryMissingData,
  retrying,
}: {
  job: ResearchJob;
  justFinished: boolean;
  onRetryMissingData: () => void;
  retrying: boolean;
}) {
  const [open, setOpen] = useState(job.status === "running" || justFinished);
  const wasRunningRef = useRef(job.status === "running");
  useEffect(() => {
    // Force the card open on either transition across the running
    // boundary while it's already on screen: idle/terminal -> running (a
    // Refresh/Retry was just triggered -- show live progress) and
    // running -> terminal (the exact moment "what happened" needs to be
    // visible, not the moment it silently collapses). Otherwise leave the
    // user's own manual collapse/expand alone.
    const nowRunning = job.status === "running";
    if (nowRunning !== wasRunningRef.current) setOpen(true);
    wasRunningRef.current = nowRunning;
  }, [job.status]);
  const isRunning = job.status === "running";
  const canRetry = !isRunning && hasRetryableGap(job);

  return (
    <div className={`card preparation-result preparation-result-${job.status}`}>
      <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>
          {HEADLINE[job.status]}
          {job.completed_at && !isRunning && (
            <span className="text-sm text-muted" style={{ fontWeight: 400 }}>
              {" "}
              · {new Date(job.completed_at).toLocaleString()}
            </span>
          )}
        </summary>
        <div style={{ marginTop: 12 }}>
          <PreparationProgress steps={job.steps} jobStatus={job.status} />
          {job.error && (
            <p className="text-sm" style={{ color: "var(--color-negative)" }}>
              {job.error}
            </p>
          )}
          {!isRunning && job.status !== "failed" && (
            <p className="text-sm text-muted" style={{ marginBottom: 0 }}>
              Research workspace is still usable with the data collected above.
            </p>
          )}
          {canRetry && (
            <button
              className="btn-secondary"
              style={{ marginTop: 12 }}
              onClick={onRetryMissingData}
              disabled={retrying}
            >
              {retrying ? "Retrying…" : "Retry Missing Data"}
            </button>
          )}
        </div>
      </details>
    </div>
  );
}
