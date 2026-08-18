import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { CompanyWorkspace } from "./pages/CompanyWorkspace";
import { EarningsEvent } from "./pages/EarningsEvent";
import { Research } from "./pages/Research";
import { HistoricalReplay } from "./pages/HistoricalReplay";
import { DataStatus } from "./pages/DataStatus";

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
          <Route path="system-status" element={<DataStatus />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
