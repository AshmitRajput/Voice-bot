import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { RecordingsResponse, CallDetailResponse } from "@/lib/types";

export interface RecordingFilters {
  search?: string;
  status?: string;
  campaignId?: string;
}

export function useRecordings(page: number, filters: RecordingFilters) {
  const params = new URLSearchParams({ page: String(page) });
  if (filters.search) params.set("search", filters.search);
  if (filters.status) params.set("status", filters.status);
  if (filters.campaignId) params.set("campaign_id", filters.campaignId);

  return useQuery({
    queryKey: ["recordings", page, filters],
    // apiFetch already prefixes "/api" itself — don't double it up here.
    queryFn: () => apiFetch<RecordingsResponse>(`/admin/recordings/?${params.toString()}`),
    placeholderData: (prev) => prev,
  });
}

export function useCallDetail(sessionId: string | undefined) {
  return useQuery({
    queryKey: ["call-detail", sessionId],
    queryFn: () => apiFetch<CallDetailResponse>(`/admin/calls/${sessionId}/`),
    enabled: !!sessionId,
  });
}
