import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";
import { Dashboard } from "@/pages/Dashboard";
import { LibrariesPage } from "@/pages/LibrariesPage";
import { QueuePage } from "@/pages/QueuePage";
import { SettingsPage } from "@/pages/SettingsPage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="queue" element={<QueuePage />} />
        {/* Anciennes pages fusionnées : liens et favoris redirigés */}
        <Route path="history" element={<Navigate to="/queue?tab=history" replace />} />
        <Route path="stats" element={<Navigate to="/" replace />} />
        <Route path="libraries" element={<LibrariesPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
