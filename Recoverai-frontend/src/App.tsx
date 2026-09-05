import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/hooks/useAuth";
import { RequireAuth } from "@/components/layout/RequireAuth";
import Login from "@/pages/auth/Login";
import ComingSoon from "@/pages/ComingSoon";
import Dashboard from "@/pages/Dashboard";
import Customers from "@/pages/Customers";
import CustomerDetails from "@/pages/CustomerDetails";
import Campaigns from "@/pages/Campaigns";
import NewCampaign from "@/pages/NewCampaign";
import CampaignDetails from "@/pages/CampaignDetails";
import RecoveryCases from "@/pages/RecoveryCases";
import Callbacks from "@/pages/Callbacks";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />

          <Route element={<RequireAuth />}>
            <Route path="/dashboard" element={<Dashboard />} />

            <Route path="/customers" element={<Customers />} />
            <Route path="/customers/:id" element={<CustomerDetails />} />

            {/* /campaigns/new must come before /campaigns/:id — otherwise
                the router matches "new" as an :id param and NewCampaign
                never renders. */}
            <Route path="/campaigns" element={<Campaigns />} />
            <Route path="/campaigns/new" element={<NewCampaign />} />
            <Route path="/campaigns/:id" element={<CampaignDetails />} />

            <Route path="/recovery-cases" element={<RecoveryCases />} />
            <Route path="/callbacks" element={<Callbacks />} />
            <Route path="/recordings" element={<ComingSoon title="Call Recordings" phase="Phase 6" />} />
            <Route path="/recordings/:sessionId" element={<ComingSoon title="Call detail" phase="Phase 6" />} />
            <Route path="/voice-test" element={<ComingSoon title="AI Voice Test" phase="already shipped" />} />
            <Route path="/personas" element={<ComingSoon title="Personas" phase="Phase 7" />} />
            <Route path="/voices" element={<ComingSoon title="Voices" phase="Phase 7" />} />
            <Route path="/knowledge" element={<ComingSoon title="Knowledge Base" phase="Phase 8" />} />
            <Route path="/settings" element={<ComingSoon title="Settings" phase="Phase 9" />} />
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
          </Route>

          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
