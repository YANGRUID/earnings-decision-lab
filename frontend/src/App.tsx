import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Company } from "./pages/Company";
import { EarningsEvent } from "./pages/EarningsEvent";
import { OptionsLab } from "./pages/OptionsLab";
import { Research } from "./pages/Research";
import { HistoricalReplay } from "./pages/HistoricalReplay";
import { DataStatus } from "./pages/DataStatus";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="company/:ticker" element={<Company />} />
          <Route path="earnings/:id" element={<EarningsEvent />} />
          <Route path="options-lab" element={<OptionsLab />} />
          <Route path="research" element={<Research />} />
          <Route path="historical-replay" element={<HistoricalReplay />} />
          <Route path="data-status" element={<DataStatus />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
