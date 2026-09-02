import { useState } from "react";
import { HistoricalEventsTab } from "../HistoricalEventsTab";
import { DecisionTab } from "../DecisionTab";
import { StrategyLabTab } from "../StrategyLabTab";
import { ExposureTab } from "../ExposureTab";
import { Notice, SectionHeader } from "../../v4/ui";

// Historical / Control (Sections 7-9, 14-16): price-reaction history first;
// then the V3-era tools, retained deliberately and labelled so none of them
// can be mistaken for official or shadow forward evidence.
export function HistoricalControlTab({ ticker }: { ticker: string }) {
  const [tool, setTool] = useState<"none" | "ondemand" | "strategy" | "exposure">("none");
  return (
    <div>
      <SectionHeader title="Historical price reaction" eyebrow="Real past earnings events" />
      <HistoricalEventsTab ticker={ticker} />

      <div className="card" style={{ marginTop: 18 }} data-testid="legacy-tools">
        <SectionHeader title="Legacy research tools" eyebrow="V3 era · not forward evidence" />
        <Notice kind="warn" testId="ondemand-notice">
          <strong>On-demand analysis — not official forward evidence.</strong> Anything generated here is a
          manual, point-in-time V3 analysis. It is stored in the AI Decision Journal and is never part of the
          official V3 control cohort or the V4 shadow forward test.
        </Notice>
        <div className="tab-bar" style={{ marginTop: 8 }}>
          <button className={`tab-button ${tool === "ondemand" ? "active" : ""}`} onClick={() => setTool(tool === "ondemand" ? "none" : "ondemand")}>On-demand V3 analysis</button>
          <button className={`tab-button ${tool === "strategy" ? "active" : ""}`} onClick={() => setTool(tool === "strategy" ? "none" : "strategy")}>V3 strategy lab</button>
          <button className={`tab-button ${tool === "exposure" ? "active" : ""}`} onClick={() => setTool(tool === "exposure" ? "none" : "exposure")}>Account exposure (read-only)</button>
        </div>
        {tool === "ondemand" && <div style={{ marginTop: 12 }}><DecisionTab ticker={ticker} /></div>}
        {tool === "strategy" && <div style={{ marginTop: 12 }}><StrategyLabTab ticker={ticker} /></div>}
        {tool === "exposure" && <div style={{ marginTop: 12 }}><ExposureTab ticker={ticker} /></div>}
      </div>
    </div>
  );
}
