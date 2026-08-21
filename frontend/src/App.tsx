import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EarningsAnalystDashboard } from "./pages/EarningsAnalystDashboard";
import { EarningsCalendarDetail } from "./pages/EarningsCalendarDetail";
import { Dashboard } from "./pages/Dashboard";
import { CompanyWorkspace } from "./pages/CompanyWorkspace";
import { EarningsEvent } from "./pages/EarningsEvent";
import { Research } from "./pages/Research";
import { HistoricalReplay } from "./pages/HistoricalReplay";
import { TrackRecord } from "./pages/TrackRecord";
import { BenchmarkTrackRecord } from "./pages/BenchmarkTrackRecord";
import { DataStatus } from "./pages/DataStatus";
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
          <Route path="earnings/:id" element={<EarningsEvent />} />
          {/* Deliberately NOT "earnings/:symbol" -- would collide with the
              int-keyed "earnings/:id" route above (a structurally different
              question: one already-reported SEC-XBRL event, vs. this
              symbol's forward-looking calendar entry). Mirrors the same
              /earnings-calendar naming the backend already uses for the
              same reason -- see api/routers/earnings_calendar.py's own
              docstring. */}
          <Route path="earnings-calendar/:symbol" element={<EarningsCalendarDetail />} />
          <Route path="research" element={<Research />} />
          <Route path="historical-replay" element={<HistoricalReplay />} />
          <Route path="track-record" element={<TrackRecord />} />
          <Route path="benchmark-track-record" element={<BenchmarkTrackRecord />} />
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
