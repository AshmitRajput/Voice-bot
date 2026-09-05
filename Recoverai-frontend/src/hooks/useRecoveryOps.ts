import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { RecoveryCasesResponse, CallbacksResponse } from "@/lib/types";

export function useRecoveryCases(status?: string, campaignId?: string) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (campaignId) params.set("campaign_id", campaignId);
  const qs = params.toString();

  return useQuery({
    queryKey: ["recovery-cases", status, campaignId],
    queryFn: () => apiFetch<RecoveryCasesResponse>(`/admin/recovery/cases/${qs ? `?${qs}` : ""}`),
  });
}

export function useCallbacks(status?: string) {
  return useQuery({
    queryKey: ["callbacks", status],
    queryFn: () =>
      apiFetch<CallbacksResponse>(`/admin/recovery/callbacks/${status ? `?status=${status}` : ""}`),
  });
}
