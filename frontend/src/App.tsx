import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EarningsAnalystDashboard } from "./pages/EarningsAnalystDashboard";
import { Dashboard } from "./pages/Dashboard";
import { CompanyWorkspace } from "./pages/CompanyWorkspace";
import { Research } from "./pages/Research";
import { DataStatus } from "./pages/DataStatus";
import { V4DecisionLab } from "./pages/V4DecisionLab";
import { V4ShadowTrackRecord } from "./pages/V4ShadowTrackRecord";
import { V4MethodologyComparison } from "./pages/V4MethodologyComparison";
import { Operations } from "./pages/Operations";
import { DataProviders } from "./pages/Settings/DataProviders";
import { AiProvider } from "./pages/Settings/AiProvider";
import { Ibkr } from "./pages/Settings/Ibkr";
import { ApiUsage } from "./pages/Settings/ApiUsage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {/* Homepage is the AI Earnings Analyst Dashboard, also reachable at
              its own canonical /dashboard URL -- same page, both paths. */}
          <Route index element={<EarningsAnalystDashboard />} />
          <Route path="dashboard" element={<EarningsAnalystDashboard />} />
          <Route path="search" element={<Dashboard />} />
          <Route path="company/:ticker" element={<CompanyWorkspace />} />
          {/* Deliberately NOT "earnings/:symbol" -- would collide with the
              int-keyed "earnings/:id" route above (a structurally different
              question: one already-reported SEC-XBRL event, vs. this
              symbol's forward-looking calendar entry). Mirrors the same
              /earnings-calendar naming the backend already uses for the
              same reason -- see api/routers/earnings_calendar.py's own
              docstring. */}
          <Route path="research" element={<Research />} />
          <Route path="operations" element={<Operations />} />
          {/* V4 forward test (internal route prefix kept as v4-shadow). Separate routes from the
              official AI Decision / Benchmark Track Record surfaces. */}
          <Route path="v4-decision-lab" element={<V4DecisionLab />} />
          <Route path="v4-decision-lab/:id" element={<V4DecisionLab />} />
          <Route path="candidate-explorer" element={<V4DecisionLab mode="explorer" />} />
          <Route path="candidate-explorer/:id" element={<V4DecisionLab mode="explorer" />} />
          <Route path="v4-shadow-track-record" element={<V4ShadowTrackRecord />} />
          <Route path="methodology-comparison" element={<V4MethodologyComparison />} />
          <Route path="settings/providers" element={<DataProviders />} />
          <Route path="settings/ai-provider" element={<AiProvider />} />
          <Route path="settings/ibkr" element={<Ibkr />} />
          <Route path="settings/usage" element={<ApiUsage />} />
          <Route path="system-status" element={<DataStatus />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
