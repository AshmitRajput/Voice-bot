import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { DashboardResponse } from "@/lib/types";

export function useDashboard(days: number) {
  return useQuery({
    queryKey: ["recovery-dashboard", days],
    queryFn: () => apiFetch<DashboardResponse>(`/admin/recovery/dashboard/?days=${days}`),
  });
}