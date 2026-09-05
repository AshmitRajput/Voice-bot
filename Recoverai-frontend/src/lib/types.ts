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


export interface CustomerBrief {
  id: number;
  name: string;
  phone_number: string;
}

export interface LLMSettingBrief {
  id: number;
  persona_name: string;
  name: string;
}

export interface Recording {
  id: number;
  session_id: string;
  customer: CustomerBrief | null;
  campaign_id: number | null;
  agent: LLMSettingBrief | null;
  status: string; // queued/ringing/ongoing/completed/failed/busy/no_answer/dropped
  intent: string;
  recovery_outcome: string;
  direction: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  // transcript/intent_history are raw JSON blobs on the model — shape
  // isn't pinned down anywhere in views_admin.py, so treat as opaque.
  // call_detail_admin's `turns` array is the structured version; prefer
  // that for rendering anything, don't parse these two.
  transcript: unknown;
  intent_history: unknown;
  call_summary: string | null;
  recording_stereo: string | null;
  recording_mixed: string | null;
}

export type RecordingsResponse = PaginatedResponse<Recording>;

// Matches call_detail_admin's turns[] dicts exactly (views_admin.py L545-554).
export interface CallTurn {
  id: number;
  speaker: string;
  text: string;
  intent: string | null;
  confidence: number | null;
  at: string | null;
}

// Matches call_detail_admin's "call" object exactly (views_admin.py L527-556).
// Note: this is a DIFFERENT shape from Recording above — no `id`, no
// `recording_stereo`, and it has pricing fields + turns[] that the list
// endpoint doesn't return. Don't assume these are interchangeable.
export interface CallDetail {
  session_id: string;
  customer: CustomerBrief | null;
  status: string;
  intent: string;
  recovery_outcome: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  transcript: unknown;
  intent_history: unknown;
  call_summary: string | null;
  recording_mixed: string | null;
  stt_pricing: string;
  tts_pricing: string;
  llm_pricing: string;
  dialer_pricing: string;
  total_cost: string;
  turns: CallTurn[];
}

export interface CallDetailResponse {
  success: boolean;
  call: CallDetail;
}

// ── TTS Voices (Phase 7) ─────────────────────────────────────────
// Matches _serialize_tts_voice exactly (views_admin.py L54-68).
export interface TTSVoice {
  id: number;
  voice_name: string;
  gender: string;
  provider_voice_id: string;
  provider_name: string;
  language: string;
  is_active: boolean;
  sample_url: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface TTSVoicesResponse {
  success: boolean;
  count: number;
  voices: TTSVoice[];
}

export interface TTSVoiceResponse {
  success: boolean;
  voice: TTSVoice;
}

// ── LLM Settings / Personas (Phase 7) ─────────────────────────────
// Matches _serialize_llm_setting exactly (views_admin.py L71-97).
// Note `voice` is the full nested TTSVoice object on read — but writes go
// through `voice_id` (see llm_settings POST / llm_setting_detail PATCH).
// Two different shapes for the same relationship, read vs write.
export interface LLMSetting {
  id: number;
  name: string;
  is_active: boolean;
  provider: string;
  model: string;
  temperature: number;
  max_tokens: number;
  persona_name: string;
  opening_line: string;
  system_prompt: string;
  behaviour: string;
  voice: TTSVoice | null;
  tone: number;
  pace: number;
  barge_in_threshold: number;
  max_turns: number;
  allow_customer_barge_in: boolean;
  language: string;
  response_max_chars: number;
  questions_per_turn_max: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface LLMSettingsResponse {
  success: boolean;
  count: number;
  settings: LLMSetting[];
}

export interface LLMSettingResponse {
  success: boolean;
  setting: LLMSetting;
}

/** Body shape for POST/PATCH /api/admin/llm-settings/ — voice_id, not voice. */
export type LLMSettingWritePayload = Partial<
  Omit<LLMSetting, "id" | "voice" | "created_at" | "updated_at">
> & { voice_id?: number };

/** Body shape for POST/PATCH /api/admin/tts-voices/. */
export type TTSVoiceWritePayload = Partial<
  Omit<TTSVoice, "id" | "created_at" | "updated_at">
>;

