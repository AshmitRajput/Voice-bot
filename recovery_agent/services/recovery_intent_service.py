"""
Recovery Intent Service — RecoverAI edition. The ONE canonical classifier. Answers only ONE question:
    "What did the customer say?"

It does NOT generate responses. It does NOT decide actions. It does NOT
take recovery actions. The orchestrator (recovery_service.py) consumes
this output and decides what to do. """

import json
import logging
import os
import re

import requests

logger = logging.getLogger('recovery_agent')


# ═══════════════════════════════════════════════════════════════
# CANONICAL RECOVERY INTENTS
# ═══════════════════════════════════════════════════════════════

INTENTS = [

    # --- Conversation opening ---
    "greeting",
    "identity_confirmed",

    # --- Payment recovery ---
    "payment_done",
    "payment_pending",
    "promise_to_pay",
    "payment_link_requested",
    "payment_link_resend_requested",
    "payment_link_received",
    "payment_link_failed",

    # --- Refusal / objection ---
    "refused_to_pay",
    "not_interested",
    "financial_hardship",
    "dispute",

    # --- Information requests ---
    "payment_amount_question",
    "payment_due_date_question",
    "payment_reason_question",
    "clarification_requested",

    # --- Follow-up scheduling ---
    "callback_requested",
    "callback_confirmed",

    # --- Exceptions ---
    "complaint",
    "wrong_number",
    "account_not_owned",

    # --- Conversation termination ---
    "goodbye",

    # --- Fallback ---
    "unclear",
]


# ═══════════════════════════════════════════════════════════════
# RECOVERY ACTIONS — separate from intent
# ═══════════════════════════════════════════════════════════════

RECOVERY_ACTIONS = {
    # Conversation opening
    "greeting":              {"action": "continue_recovery_conversation", "filler": False, "next_step": "state_purpose"},
    "identity_confirmed":    {"action": "proceed_with_recovery",          "filler": False, "next_step": "explain_balance"},

    # Payment recovery
    "payment_done":                  {"action": "verify_payment",            "filler": True,  "next_step": "verification"},
    "payment_pending":               {"action": "understand_blocker",         "filler": False, "next_step": "ask_reason"},
    "promise_to_pay":                {"action": "record_payment_promise",    "filler": True,  "next_step": "confirm_date"},
    "payment_link_requested":        {"action": "send_payment_link",         "filler": True,  "next_step": "confirm_delivery"},
    "payment_link_resend_requested": {"action": "resend_payment_link",       "filler": True,  "next_step": "confirm_delivery"},
    "payment_link_received":         {"action": "confirm_next_payment_step", "filler": False, "next_step": "wait_or_close"},
    "payment_link_failed":           {"action": "handle_link_failure",       "filler": True,  "next_step": "generate_new_link"},

    # Refusal / objection
    "refused_to_pay":      {"action": "handle_payment_refusal",     "filler": False, "next_step": "understand_reason"},
    "not_interested":      {"action": "respect_decline",            "filler": False, "next_step": "end_or_close_recovery"},
    "financial_hardship":  {"action": "offer_payment_extension",    "filler": False, "next_step": "ask_expected_date"},
    "dispute":             {"action": "pause_recovery",             "filler": False, "next_step": "escalate_dispute"},

    # Information
    "payment_amount_question":    {"action": "retrieve_verified_balance",      "filler": False, "next_step": "explain"},
    "payment_due_date_question":  {"action": "retrieve_due_date",              "filler": False, "next_step": "explain"},
    "payment_reason_question":    {"action": "retrieve_verified_obligation",   "filler": False, "next_step": "explain"},
    "clarification_requested":    {"action": "retrieve_verified_context",      "filler": False, "next_step": "generate_explanation"},

    # Callback
    "callback_requested":  {"action": "schedule_recovery_callback", "filler": True,  "next_step": "confirm_callback_time"},
    "callback_confirmed":  {"action": "confirm_scheduled_callback", "filler": False, "next_step": "wrap_up"},

    # Exceptions
    "complaint":         {"action": "handle_complaint",         "filler": False, "next_step": "escalate"},
    "wrong_number":      {"action": "mark_wrong_number",        "filler": False, "next_step": "end_call"},
    "account_not_owned": {"action": "mark_account_not_owned",   "filler": False, "next_step": "end_call"},

    # Termination
    "goodbye":  {"action": "end_call",       "filler": False, "next_step": "finalize_call"},

    # Fallback
    "unclear":  {"action": "ask_clarification", "filler": False, "next_step": "rephrase_or_clarify"},
}


# Call OUTCOME enum — what HAPPENED in this call (separate from intent). # Set post-call by recovery_service, NOT by the live classifier. CALL_OUTCOMES = [
    "recovered",                     # payment verified + case closed
    "promise_to_pay",                # PTP recorded
    "payment_link_sent",             # link generated/sent successfully
    "payment_already_completed",     # customer's payment_done verified
    "refused_to_pay",                # explicit refusal recorded
    "disputed",                      # amount/obligation dispute raised
    "callback_scheduled",            # future follow-up booked
    "complaint_escalated",           # escalation case created
    "wrong_number",                  # number doesn't belong to customer
    "account_not_owned",             # customer says account isn't theirs
    "customer_hung_up",              # call disconnected mid-conversation
    "no_answer",
    "busy",
    "voicemail",
    "technical_failure",
    "unclear",
]


# Recovery STATUS — where the case IS in the recovery lifecycle. RECOVERY_STATUSES = [
    "pending",
    "in_progress",
    "payment_verified",
    "promise_recorded",
    "payment_link_sent",
    "callback_scheduled",
    "refused",
    "disputed",
    "complaint",
    "recovered",
    "follow_up_required",
    "closed",
]


# ═══════════════════════════════════════════════════════════════
# CLASSIFIER
# ═══════════════════════════════════════════════════════════════

CLASSIFICATION_PROMPT = """You are a strict intent classifier for a Hindi/Hinglish revenue-recovery voice agent. Your ONLY job is to read the customer's LAST utterance and output a JSON object with:
  - intent:    ONE of the canonical intents below
  - confidence: 0.0 to 1.0
  - entities:  any structured values you can extract (dates, times, reasons)

You must NEVER:
  - answer the customer
  - suggest actions
  - choose from intents not in this list

═══════════════════════════════════════════════════════════════
CANONICAL INTENTS
═══════════════════════════════════════════════════════════════

CONVERSATION OPENING
- greeting:                "Hello", "Haan boliye", "Ji?"
- identity_confirmed:      "Haan main Rahul bol raha hoon", "Yes speaking"

PAYMENT RECOVERY
- payment_done:                  Customer claims payment is already made. - payment_pending:               Customer acknowledges payment is still pending. - promise_to_pay:                Customer commits to paying at a future time. → entities.promise_date, entities.promise_time
- payment_link_requested:        Customer wants a payment link sent. - payment_link_resend_requested: Customer wants the link sent again / link expired. - payment_link_received:         Customer confirms they received the link. - payment_link_failed:           Customer says the link isn't working / errored. REFUSAL / OBJECTION
- refused_to_pay:       Customer explicitly says they will not pay. - not_interested:       Customer doesn't want to continue this conversation. - financial_hardship:   Customer cites money problems as the reason. - dispute:              Customer disputes the amount or the obligation itself. INFORMATION
- payment_amount_question:    "Kitna pay karna hai?"
- payment_due_date_question:  "Last date kab hai?"
- payment_reason_question:    "Ye payment kisliye hai?"
- clarification_requested:    "Thoda detail mein batao", "Samjhao"

FOLLOW-UP
- callback_requested:  Customer asks to be called later. → entities.callback_date, entities.context_callback_time
- callback_confirmed:  Customer confirms a previously proposed callback. EXCEPTIONS
- complaint:         Customer has a complaint (about service, calls, staff, etc.). - wrong_number:      Number doesn't belong to the intended customer. - account_not_owned: Customer says the account/vehicle is no longer theirs. TERMINATION
- goodbye:           Natural conversation ending. FALLBACK
- unclear:           The utterance is ambiguous, off-topic, or uninterpretable. ═══════════════════════════════════════════════════════════════
OUTPUT FORMAT (strict JSON, nothing else)
═══════════════════════════════════════════════════════════════

{{
  "intent": "<one of the canonical intents>",
  "confidence": <0.0-1.0>,
  "entities": {{
    "promise_date": "<YYYY-MM-DD if mentioned, else omit>",
    "promise_time": "<HH:MM if mentioned, else omit>",
    "callback_date": "<YYYY-MM-DD if mentioned, else omit>",
    "callback_time": "<HH:MM if mentioned, else omit>",
    "reason": "<short reason string if intent is complaint/dispute/refusal, else omit>"
  }}
}}

═══════════════════════════════════════════════════════════════
RECENT CONVERSATION (for context only — classify the LAST customer line)
═══════════════════════════════════════════════════════════════
{history}

═══════════════════════════════════════════════════════════════
LAST CUSTOMER UTTERANCE
═══════════════════════════════════════════════════════════════
"{customer_text}"
"""


# Lightweight keyword-based fallback (works even without an LLM)
# Maps Hindi/Hinglish keyword patterns -> intent. Used when no LLM key
# is configured OR the LLM call fails. #

_HINDI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _kw_match(text, patterns):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


_KEYWORD_RULES = [
    # Payment
    ("payment_done", [
        r"\b(paid|pay kar diya|kar diya hai|kar chuka|kar chuki|bhar diya|bhar di)\b",
        r"payment (ho gaya|kar diya|karta|karke)\b",
    ]),
    ("payment_pending", [
        r"\b(pending|baki|baaki|nahi kiya|nahi hua|nahi kar paya)\b",
        r"abhi tak nahi|abhi nahi\b",
    ]),
    ("promise_to_pay", [
        r"\b(kal|parson|aaj|agle|next|promise|kar dunga|kar dungi|karunga|karungi)\b",
        r"\d+\s*(tareekh|tarikh|ko|date)\b",
    ]),
    ("payment_link_requested", [
        r"\b(link bhej|link chahiye|link do|payment link|upi link|qr code)\b",
    ]),
    ("payment_link_resend_requested", [
        r"\b(phir se|dobara|resend|link expire|link nahi mila|link nahi aaya)\b",
    ]),
    ("payment_link_received", [
        r"\b(link aa gaya|mil gaya|link mila|received|got it)\b",
    ]),
    ("payment_link_failed", [
        r"\b(link (kharab|crash|error|open nahi)|error aa|page nahi khul)\b",
    ]),

    # Refusal
    ("refused_to_pay", [
        r"\b(pay nahi karunga|nahi dunga|nahi dungi|nahi karunga|nahi karungi|nahi dena|nahi bharna)\b",
        r"main (nahi|nहीं) (dunga|karunga|bharunga|pay karunga)\b",
    ]),
    ("not_interested", [
        r"\b(interest nahi|fark nahi|mat karo|rehne do|chhod do)\b",
    ]),
    ("financial_hardship", [
        r"\b(paise nahi|job chali|naukri gayi|money problem|financial|garib|gareeb)\b",
    ]),
    ("dispute", [
        r"\b(galat hai|galat amount|amount galat|bill galat|dispute|kya charge|kab ka hai|main (nahi) (dunga|paya))\b",
    ]),

    # Information
    ("payment_amount_question", [
        r"\b(kitna|kitne|kya amount|kitne (ka|paise|rupees))\b",
    ]),
    ("payment_due_date_question", [
        r"\b(kab tak|last date|due date|deadline|kab tak bharna|deadline kab)\b",
    ]),
    ("payment_reason_question", [
        r"\b(kisliye|kis baare|kya hai ye|kyun|kyu|kyunki|reason)\b",
    ]),
    ("clarification_requested", [
        r"\b(samjhao|samjha do|detail mein|thoda aur|explain|clarify|clear karo)\b",
    ]),

    # Callback
    ("callback_requested", [
        r"\b(call back|baad mein call|phir call|kal call|shaam ko call|agle hafte|next week)\b",
        r"\b(callback|baad mein|fir kab)\b",
    ]),
    ("callback_confirmed", [
        r"\b(haan|theek hai|ok|okay|thik hai|chalega)\b",
    ]),

    # Exceptions
    ("complaint", [
        r"\b(shikayat|complaint|pareshan|tang|naraaz|ganda|kharab service|worst)\b",
    ]),
    ("wrong_number", [
        r"\b(galat number|galat number hai|ye number nahi|kiska number)\b",
    ]),
    ("account_not_owned", [
        r"\b(mera nahi|account mera nahi|naam alag|galti se|wrong person)\b",
    ]),

    # Termination
    ("goodbye", [
        r"^\s*(bye|goodbye|tata|alvida|phir milenge|baad mein milte)\b",
    ]),

    # Conversation
    ("greeting", [
        r"^\s*(namaste|namaskar|hello|hi|hey)\b",
    ]),
    ("identity_confirmed", [
        r"\b(haan main|main hi|yes speaking|main bol raha|main bol rahi|main hi bol)\b",
    ]),
]


class RecoveryIntentService:
    """
    The single canonical intent classifier for the RecoverAI voice agent. """

    def __init__(self):
        self.intents = INTENTS
        self._gemini_key = os.environ.get("GOOGLE_API_KEY", "")
        self._use_llm = bool(self._gemini_key)

    # ───────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────

    def detect_intent(self, customer_text, history=None):
        """
        Classify the customer's last utterance. Returns:

            {
                "intent":      <canonical intent>,
                "confidence":  <float 0..1>,
                "entities":    <dict>,
                "raw":         <raw model text, for debugging>,
            }
        """
        if self._use_llm:
            try:
                return self._classify_with_llm(customer_text, history or [])
            except Exception as e:
                logger.warning(
                    f"[INTENT] LLM classification failed: {e}; falling back to keywords"
                )
        return self._classify_with_keywords(customer_text)

    def get_response_strategy(self, intent):
        """
        Return the recovery-action strategy for a given intent. The orchestrator still chooses whether to actually invoke it —
        this is the default mapping only. """
        return RECOVERY_ACTIONS.get(
            intent,
            {"action": "general_chat", "filler": False, "next_step": "clarify"},
        )

    # ───────────────────────────────────────────────────────────
    # Internals
    # ───────────────────────────────────────────────────────────

    def _format_history(self, history):
        if not history:
            return ""
        lines = []
        for turn in history[-6:]:
            role = turn.get("role") or turn.get("speaker", "")
            text = turn.get("text", "")
            if not text:
                continue
            lines.append(f"{role}: {text}")
        return "\n".join(lines)

    def _classify_with_llm(self, customer_text, history):
        """Use Gemini (or any OpenAI-compatible LLM via GOOGLE_API_KEY) to classify."""
        history_str = self._format_history(history)
        prompt = CLASSIFICATION_PROMPT.format(
            history=history_str or "(no prior context)",
            customer_text=customer_text,
        )

        # Gemini REST API (no SDK needed)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={self._gemini_key}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 200,
            },
        }
        resp = requests.post(url, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = self._parse_json_response(raw)
        validated = self._validate_and_normalize(parsed)
        validated["raw"] = raw
        return validated

    def _classify_with_keywords(self, customer_text):
        """Pure-Python keyword-based fallback (works without any LLM)."""
        text = customer_text or ""
        # Pick the FIRST matching rule (rules are ordered roughly by priority)
        for intent, patterns in _KEYWORD_RULES:
            if _kw_match(text, patterns):
                return {
                    "intent": intent,
                    "confidence": 0.7,
                    "entities": {},
                    "raw": f"[keyword-match: {intent}]",
                }
        return {
            "intent": "unclear",
            "confidence": 0.0,
            "entities": {},
            "raw": "",
        }

    def _parse_json_response(self, text):
        """Tolerate fences / extra prose; pull the first JSON object out."""
        if not text:
            return {}
        text = text.strip()

        # Strip ```json fences if present
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("`").strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Last-resort: locate the first {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning(f"[INTENT] could not parse JSON from classifier: {text!r}")
        return {}

    def _validate_and_normalize(self, parsed):
        intent = parsed.get("intent")
        if intent not in INTENTS:
            logger.warning(
                f"[INTENT] classifier returned unknown intent '{intent}'; "
                f"normalising to 'unclear'"
            )
            intent = "unclear"

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        entities = parsed.get("entities") or {}
        if not isinstance(entities, dict):
            entities = {}

        return {
            "intent": intent,
            "confidence": confidence,
            "entities": entities,
        }


# Module-level singleton
recovery_intent_service = RecoveryIntentService()

# Back-compat: also expose as `intent_service` for old imports
intent_service = recovery_intent_service