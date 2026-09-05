import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";
import { AppShell } from "./AppShell";
import { Loader } from "./Loader";

/**
 * Phase 0: gates on the stub `user` from useAuth (nothing is verified
 * server-side yet). Phase 1 swaps the stub for a real session check and
 * this component's shape shouldn't need to change.
 */
export function RequireAuth() {
  const { user, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) return <Loader label="Checking your session…" />;
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;

  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}
