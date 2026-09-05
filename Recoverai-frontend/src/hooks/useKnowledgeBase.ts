import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type {
  KnowledgeDocumentsResponse,
  KnowledgeDocumentDetailResponse,
  KnowledgeStatsResponse,
  KbStorePayload,
} from "@/lib/types";

export function useKnowledgeDocuments(category?: string, status?: string) {
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  if (status) params.set("status", status);
  const qs = params.toString();

  return useQuery({
    queryKey: ["kb-documents", category, status],
    queryFn: () => apiFetch<KnowledgeDocumentsResponse>(`/kb/documents/${qs ? `?${qs}` : ""}`),
  });
}

export function useKnowledgeStats() {
  return useQuery({
    queryKey: ["kb-stats"],
    queryFn: () => apiFetch<KnowledgeStatsResponse>("/kb/stats/"),
  });
}

export function useKnowledgeDocumentDetail(docId: string | undefined) {
  return useQuery({
    queryKey: ["kb-document-detail", docId],
    queryFn: () => apiFetch<KnowledgeDocumentDetailResponse>(`/kb/documents/${docId}/`),
    enabled: !!docId,
  });
}

// kb_store doubles as create AND update — same endpoint either way,
// keyed by doc_id (update_or_create on the backend). If you don't pass a
// doc_id, the backend generates one (`doc_{timestamp}`) and this won't
// know it to invalidate the single-document cache, but the list/stats
// queries below still refresh correctly.
export function useStoreKnowledgeDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: KbStorePayload) =>
      apiFetch<{ success: boolean; kb_document_id?: number }>("/kb/store/", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["kb-stats"] });
    },
  });
}

export function useDeleteKnowledgeDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (docId: string) =>
      apiFetch<{ success: boolean }>(`/kb/documents/${docId}/`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["kb-stats"] });
    },
  });
}