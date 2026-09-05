import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import type { Customer, CustomerDetailResponse, PaginatedResponse } from "@/lib/types";

export function useCustomers(page: number, search: string) {
  return useQuery({
    queryKey: ["customers", page, search],
    queryFn: () =>
      apiFetch<PaginatedResponse<Customer>>(
        `/admin/customers/?page=${page}&page_size=25${
          search ? `&search=${encodeURIComponent(search)}` : ""
        }`
      ),
    placeholderData: (prev) => prev,
  });
}

export function useCustomerDetail(id: string | undefined) {
  return useQuery({
    queryKey: ["customer-detail", id],
    queryFn: () => apiFetch<CustomerDetailResponse>(`/admin/customers/${id}/`),
    enabled: !!id,
  });
}
