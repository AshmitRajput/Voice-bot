import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { TTSVoicesResponse, TTSVoiceResponse, TTSVoiceWritePayload } from "@/lib/types";

export function useVoices() {
  return useQuery({
    queryKey: ["voices"],
    queryFn: () => apiFetch<TTSVoicesResponse>("/admin/tts-voices/"),
  });
}

export function useCreateVoice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: TTSVoiceWritePayload) =>
      apiFetch<TTSVoiceResponse>("/admin/tts-voices/", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["voices"] }),
  });
}

export function useUpdateVoice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: TTSVoiceWritePayload }) =>
      apiFetch<TTSVoiceResponse>(`/admin/tts-voices/${id}/`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["voices"] }),
  });
}

export function useDeleteVoice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch(`/admin/tts-voices/${id}/`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["voices"] }),
  });
}

/** POST /api/admin/test-tts/ — returns base64 audio to preview a voice inline. */
export function useTestTts() {
  return useMutation({
    mutationFn: (payload: { text?: string; provider: string; voice: string }) =>
      apiFetch<{ success: boolean; audio_b64: string; provider: string; voice: string }>(
        "/admin/test-tts/",
        { method: "POST", body: JSON.stringify(payload) },
      ),
  });
}
