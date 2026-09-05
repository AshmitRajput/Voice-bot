/** Mirrors GET /api/admin/recovery/dashboard/?days=&campaign_id= exactly. */
export interface DashboardTotals {
  total_calls: number;
  calls_attempted: number;
  calls_connected: number;
  connection_rate: number;
  complaints: number;
  callbacks: number;
  declines: number;
  wrong_numbers: number;
  avg_duration_seconds: number;
}

export interface DashboardIntentCount {
  intent: string;
  count: number;
}

export interface DashboardOutcomeCount {
  outcome: string;
  count: number;
}

export interface DashboardCosts {
  stt: string;
  tts: string;
  llm: string;
  dialer: string;
  total: string;
}

export interface DashboardResponse {
  success: boolean;
  period_days: number;
  totals: DashboardTotals;
  by_intent: DashboardIntentCount[];
  by_outcome: DashboardOutcomeCount[];
  costs: DashboardCosts;
  recovery: { amount_recovered: string };
}


export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

// ── Customers ──────────────────────────────────────────────────
// Matches customers_list's per-row dict exactly.
export interface Customer {
  id: number;
  name: string;
  phone_number: string;
  account_reference: string;
  do_not_call: boolean;
  total_calls: number;
  created_at: string | null;
}

// Matches customer_detail's extra fields (on top of Customer).
export interface CustomerDetail extends Customer {
  email: string | null;
  external_customer_id: string | null;
  preferred_language: string | null;
  do_not_call_reason: string | null;
}

export interface CustomerDetailResponse {
  success: boolean;
  customer: CustomerDetail;
  recovery_cases: RecoveryCase[];
}

// ── Recovery cases ──────────────────────────────────────────────
// Matches _serialize_recovery_case exactly. Note: no campaign_name here —
// only campaign_id. If you want the campaign name shown against a case,
// join client-side against the campaigns list, or ask backend to add it.
export interface RecoveryCase {
  id: number;
  customer_id: number;
  campaign_id: number | null;
  status: string; // e.g. "open" | "closed" — exact choices are in models.py
  priority: string;
  outcome: string | null;
  current_intent: string | null;
  current_outcome: string | null;
  amount_due: string;
  amount_recovered: string;
  due_date: string | null;
  promise_date: string | null;
  created_at: string | null;
  closed_at: string | null;
}

export interface RecoveryCasesResponse {
  success: boolean;
  count: number;
  cases: RecoveryCase[];
}

// ── Campaigns ───────────────────────────────────────────────────
// Matches the campaigns() list serializer.
export interface Campaign {
  id: number;
  name: string;
  campaign_type: string;
  status: string; // "draft" | ... (draft is the only value views_admin.py sets on create)
  customer_count: number;
  calls_attempted: number;
  cases_recovered: number;
  amount_recovered: string;
  created_at: string | null;
  started_at: string | null;
}

// Matches campaign_detail's GET response (superset of Campaign).
export interface CampaignDetail extends Campaign {
  description: string;
  calls_connected: number;
  finished_at: string | null;
}

export interface CampaignsResponse {
  success: boolean;
  count: number;
  campaigns: Campaign[];
}

export interface CampaignDetailResponse {
  success: boolean;
  campaign: CampaignDetail;
}

export interface Callback {
  id: number;
  customer_id: number;
  recovery_case_id: number;
  scheduled_for: string | null;
  reason: string;
  status: string; // e.g. "requested" | "confirmed" | "completed" | "missed" — exact choices in models.py
  session_id: string | null;
  created_at: string | null;
}

export interface CallbacksResponse {
  success: boolean;
  count: number;
  callbacks: Callback[];
}

