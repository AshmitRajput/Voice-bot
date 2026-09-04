"""
Recovery Intent Service — the ONE canonical classifier for RecoverAI.

This file answers ONLY one question:
    "What did the customer say?"

It does NOT generate responses. It does NOT decide actions. It does NOT
take recovery actions. It ONLY returns intent + confidence + entities.

The orchestrator (recovery_service.py) consumes this output and decides
what to do. """

import json
import logging
import os

import requests

from .llm_service import llm_service

logger = logging.getLogger('voice_bot')


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

# Maps intent -> suggested recovery action. The orchestrator still has
# final say (and may override based on context), but this is the default.

RECOVERY_ACTIONS = {
    # Conversation opening
    "greeting":              {"action": "continue_recovery_conversation", "filler": False},
    "identity_confirmed":    {"action": "proceed_with_recovery",          "filler": False},

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


# Call OUTCOME enum — what HAPPENED in this call (separate from intent).
# Set post-call by recovery_service, NOT by the live classifier.

CALL_OUTCOMES = [
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


# Recovery STATUS — where the case IS in the recovery lifecycle.

RECOVERY_STATUSES = [
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

CLASSIFICATION_PROMPT = """You are a strict intent classifier for a Hindi/Hinglish revenue-recovery voice agent.

Your ONLY job is to read the customer's LAST utterance and output a JSON object with:
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
- payment_done:                  Customer claims payment is already made.
- payment_pending:               Customer acknowledges payment is still pending.
- promise_to_pay:                Customer commits to paying at a future time.
                                 → entities.promise_date, entities.promise_time
- payment_link_requested:        Customer wants a payment link sent.
- payment_link_resend_requested: Customer wants the link sent again / link expired.
- payment_link_received:         Customer confirms they received the link.
- payment_link_failed:           Customer says the link isn't working / errored.

REFUSAL / OBJECTION
- refused_to_pay:       Customer explicitly says they will not pay.
- not_interested:       Customer doesn't want to continue this conversation.
- financial_hardship:   Customer cites money problems as the reason.
- dispute:              Customer disputes the amount or the obligation itself.

INFORMATION
- payment_amount_question:    "Kitna pay karna hai?"
- payment_due_date_question:  "Last date kab hai?"
- payment_reason_question:    "Ye payment kisliye hai?"
- clarification_requested:    "Thoda detail mein batao", "Samjhao"

FOLLOW-UP
- callback_requested:  Customer asks to be called later.
                       → entities.callback_date, entities.context_callback_time
- callback_confirmed:  Customer confirms a previously proposed callback.

EXCEPTIONS
- complaint:         Customer has a complaint (about service, calls, staff, etc.).
- wrong_number:      Number doesn't belong to the intended customer.
- account_not_owned: Customer says the account/vehicle is no longer theirs.

TERMINATION
- goodbye:           Natural conversation ending.

FALLBACK
- unclear:           The utterance is ambiguous, off-topic, or uninterpretable.

═══════════════════════════════════════════════════════════════
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


class RecoveryIntentService:
    """
    The single canonical intent classifier for the RecoverAI voice agent. """

    def __init__(self):
        self.intents = INTENTS

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
        history_str = self._format_history(history or [])
        prompt = CLASSIFICATION_PROMPT.format(
            history=history_str or "(no prior context)",
            customer_text=customer_text,
        )

        result = llm_service.generate(
            prompt=prompt,
            system_prompt=(
                "You are a strict intent classifier. Output only valid JSON. "
                "Never write explanations, never write conversational text. "
                "Never invent intents outside the provided list."
            ),
            max_tokens=200,
            temperature=0.0,
        )

        if not result.get("success"):
            logger.warning(
                f"[INTENT] classifier call failed: {result.get('error')}; "
                f"falling back to 'unclear'"
            )
            return {
                "intent": "unclear",
                "confidence": 0.0,
                "entities": {},
                "raw": "",
            }

        raw_text = result.get("text", "")
        parsed = self._parse_json_response(raw_text)
        validated = self._validate_and_normalize(parsed)

        validated["raw"] = raw_text
        return validated

    def get_response_strategy(self, intent):
        """
        Return the recovery-action strategy for a given intent.
        The orchestrator still chooses whether to actually invoke it —
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


# Module-level singleton — referenced from views.py / recovery_service.py
recovery_intent_service = RecoveryIntentService()