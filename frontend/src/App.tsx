import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { CompanyWorkspace } from "./pages/CompanyWorkspace";
import { EarningsEvent } from "./pages/EarningsEvent";
import { Research } from "./pages/Research";
import { HistoricalReplay } from "./pages/HistoricalReplay";
import { TrackRecord } from "./pages/TrackRecord";
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
          <Route index element={<Dashboard />} />
          <Route path="company/:ticker" element={<CompanyWorkspace />} />
          <Route path="earnings/:id" element={<EarningsEvent />} />
          <Route path="research" element={<Research />} />
          <Route path="historical-replay" element={<HistoricalReplay />} />
          <Route path="track-record" element={<TrackRecord />} />
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
