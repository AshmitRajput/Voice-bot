import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { LLMSettingsResponse, LLMSettingResponse, LLMSettingWritePayload } from "@/lib/types";

export function usePersonas() {
  return useQuery({
    queryKey: ["personas"],
    queryFn: () => apiFetch<LLMSettingsResponse>("/admin/llm-settings/"),
  });
}

export function useCreatePersona() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: LLMSettingWritePayload) =>
      apiFetch<LLMSettingResponse>("/admin/llm-settings/", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["personas"] }),
  });
}

export function useUpdatePersona() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: LLMSettingWritePayload }) =>
      apiFetch<LLMSettingResponse>(`/admin/llm-settings/${id}/`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["personas"] }),
  });
}
