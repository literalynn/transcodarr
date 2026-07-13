import { Route, Routes } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";
import { Dashboard } from "@/pages/Dashboard";
import { HistoryPage } from "@/pages/HistoryPage";
import { LibrariesPage } from "@/pages/LibrariesPage";
import { QueuePage } from "@/pages/QueuePage";
import { SettingsPage } from "@/pages/SettingsPage";
import { StatsPage } from "@/pages/StatsPage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="history" element={<HistoryPage />} />
        <Route path="stats" element={<StatsPage />} />
        <Route path="libraries" element={<LibrariesPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
