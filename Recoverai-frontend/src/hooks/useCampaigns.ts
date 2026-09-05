import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { CampaignsResponse, CampaignDetailResponse } from "@/lib/types";

export function useCampaigns(status?: string) {
  return useQuery({
    queryKey: ["campaigns", status],
    queryFn: () =>
      apiFetch<CampaignsResponse>(
        `/admin/campaigns/${status ? `?status=${status}` : ""}`
      ),
  });
}

export function useCampaignDetail(id: string | undefined) {
  return useQuery({
    queryKey: ["campaign-detail", id],
    queryFn: () => apiFetch<CampaignDetailResponse>(`/admin/campaigns/${id}/`),
    enabled: !!id,
  });
}

interface NewCampaignInput {
  name: string;
  campaign_type: string;
  description?: string;
  target_due_within_days?: number;
}

export function useCreateCampaign() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewCampaignInput) =>
      apiFetch<{ success: boolean; campaign: { id: number; name: string; status: string } }>(
        `/admin/campaigns/`,
        { method: "POST", body: JSON.stringify(body) }
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });
}

export function useUpdateCampaign(id: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<NewCampaignInput> & { status?: string }) =>
      apiFetch<{ success: boolean }>(`/admin/campaigns/${id}/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["campaign-detail", id] });
      qc.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });
}
