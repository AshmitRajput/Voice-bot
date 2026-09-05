import { BrowserRouter, Routes, Route, Navigate, Outlet } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import Dashboard from "@/routes/_app.dashboard";
import VoiceTestPage from "@/components/voice-test/VoiceTestPage";

/**
 * Step 2 of the migration plan: only /dashboard and /voice-test exist.
 * Add each further route (Customers, Campaigns, Personas, ...) here as
 * its page component actually lands — an unbuilt link is worse than a
 * missing one.
 */
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell><Outlet /></AppShell>}>
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/voice-test" element={<VoiceTestPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
