import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { CallSettings, CallSettingsResponse } from "@/lib/types";

export function useCallSettings() {
  return useQuery({
    queryKey: ["call-settings"],
    queryFn: () => apiFetch<CallSettingsResponse>("/admin/call-settings/"),
  });
}

export function useUpdateCallSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: Partial<CallSettings>) =>
      apiFetch<CallSettingsResponse>("/admin/call-settings/", {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["call-settings"] }),
  });
}
