"""
Recovery Service — the orchestrator (plan doc §13).

This is the business brain. It does NOT talk to Chroma, the LLM client,
or STT/TTS directly. It:
    1. assembles verified context for a turn (via crm_service)
    2. takes an already-classified intent (from the LLM turn's structured
       output — see cloud_llm_service.chat_turn / Option A — or from
       recovery_intent_service as a fallback classifier) and dispatches
       to the correct handler
    3. calls payment_service / callback logic for the actual side effects
    4. writes RecoveryEvent + updates RecoveryCase via crm_service
    5. never writes PaymentRecord directly from customer speech — only
       payment_service (backed by the real provider) may do that

The LLM must never be the source of truth for payment or case state;
this module is.
"""

import logging
from datetime import datetime, date

from django.utils import timezone

from .crm_service import crm_service
from .payment_service import payment_service
from .recovery_intent_service import recovery_intent_service
from .cloud_llm_service import chat_turn

logger = logging.getLogger('recovery_agent')


class RecoveryService:

    # ───────────────────────────────────────────────────────────
    # Context assembly — fed to the LLM as `context` in chat_turn/chat_turn_stream
    # ───────────────────────────────────────────────────────────
    def process_turn(self, session_id, customer_text, customer_id=None, history=None,
                      call_session_id=None):
        """
        Full non-streaming turn used by /api/test/process-turn/. The real
        voice path (consumers.py) does NOT call this -- it streams via
        chat_turn_stream and lets the LLM's tool-calling loop invoke
        recovery tools directly. This exists for text-only testing.
        """
        history = history or []

        classification = recovery_intent_service.detect_intent(customer_text, history=history)
        intent = classification["intent"]
        entities = classification.get("entities", {})
        entities["confidence"] = classification.get("confidence", 0.0)

        context = None
        if customer_id:
            context = self.get_recovery_context(customer_id, call_session_id=call_session_id)
        if context is None:
            context = {
                "customer_id": customer_id,
                "customer_name": "Customer",
                "recovery_case_id": None,
                "recovery_status": "no_open_case",
                "amount_due": "0",
                "outstanding_amount": "0",
                "due_date": None,
                "workflow": "revenue_recovery",
            }

        dispatch_result = self.handle_intent(
            intent, entities, context, call_session_id=call_session_id,
        )

        llm_result = chat_turn(
            session_id=session_id,
            customer_text=customer_text,
            context=context,
            history=history,
            use_rag=True,
        )

        return {
            "intent": intent,
            "confidence": classification.get("confidence", 0.0),
            "entities": entities,
            "recovery_result": dispatch_result,
            "response_text": llm_result.get("response_text", ""),
            "usage": llm_result.get("usage", {}),
            "recovery_status": context.get("recovery_status"),
        }
    
    
    def get_recovery_context(self, customer_id, call_session_id=None):
        profile = crm_service.get_recovery_profile(customer_id)
        if profile is None:
            return None

        case = profile.get("open_case")
        balance = crm_service.get_outstanding_balance(customer_id)
        due = crm_service.get_payment_due_date(customer_id)
        payment_status = crm_service.get_payment_status(customer_id)

        return {
            "customer_id": profile["customer_id"],
            "customer_name": profile["customer_name"],
            "phone_number": profile["phone_number"],
            "preferred_language": profile["preferred_language"],
            "do_not_call": profile["do_not_call"],

            "recovery_case_id": case["id"] if case else None,
            "recovery_status": case["status"] if case else "no_open_case",
            "current_outcome": case["current_outcome"] if case else "",
            "amount_due": case["amount_due"] if case else balance["outstanding_amount"],
            "outstanding_amount": balance["outstanding_amount"],
            "due_date": due.get("due_date"),
            "payment_status": payment_status["status"],

            "call_session_id": call_session_id,
            "workflow": "revenue_recovery",
            "today": timezone.localdate().isoformat(),
            "current_datetime_ist": timezone.localtime().isoformat(),
        }

    # ───────────────────────────────────────────────────────────
    # Main dispatch — call this once per turn with the classified intent
    # ───────────────────────────────────────────────────────────

    def handle_intent(self, intent, entities, context, call_session_id=None):
        """
        intent   : one of recovery_intent_service.INTENTS
        entities : dict extracted alongside the intent (promise_date, etc.)
        context  : dict from get_recovery_context()
        Returns a dict the caller can fold back into the LLM's next turn /
        tool-result, e.g. {"handled": True, "recovery_status": "..."}.
        """
        case_id = context.get("recovery_case_id")
        customer_id = context["customer_id"]

        handler = self._HANDLERS.get(intent, self._handle_default)
        try:
            result = handler(self, intent, entities or {}, context, case_id, customer_id, call_session_id)
        except Exception as exc:
            logger.error(f"[RECOVERY] handler for intent={intent} failed: {exc}", exc_info=True)
            result = {"handled": False, "error": str(exc)}

        if case_id:
            crm_service.record_recovery_event(
                case_id=case_id, event_type=result.get("event_type", intent),
                intent=intent, confidence=entities.get("confidence", 0.0),
                payload=entities, call_session_id=call_session_id,
            )
        return result

    # ───────────────────────────────────────────────────────────
    # Individual handlers
    # ───────────────────────────────────────────────────────────

    def _handle_payment_done(self, intent, entities, context, case_id, customer_id, call_session_id):
        # Customer SAYS they paid — this is a signal only. We do NOT mark
        # PaymentRecord as paid here. A real implementation calls
        # payment_service.verify_payment() against the provider/webhook.
        status = crm_service.get_payment_status(customer_id)
        if status.get("status") == "paid":
            if case_id:
                crm_service.update_case_status(case_id, status='closed', outcome='recovered',
                                                current_intent=intent)
            return {"handled": True, "event_type": "payment_verified", "verified": True}
        return {
            "handled": True, "event_type": "payment_status_checked", "verified": False,
            "message": "customer claims payment done; provider does not yet confirm it",
        }

    def _handle_payment_pending(self, intent, entities, context, case_id, customer_id, call_session_id):
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', current_intent=intent)
        return {"handled": True, "event_type": "payment_status_checked"}

    def _handle_promise_to_pay(self, intent, entities, context, case_id, customer_id, call_session_id):
        promise_date = self._parse_date(entities.get("promise_date"))
        if case_id:
            crm_service.update_case_status(
                case_id, status='in_progress', outcome='promise_recorded',
                current_intent=intent, promise_date=promise_date,
            )
        return {"handled": True, "event_type": "promise_recorded", "promise_date": entities.get("promise_date")}

    def _handle_payment_link(self, intent, entities, context, case_id, customer_id, call_session_id):
        payment_status = crm_service.get_payment_status(customer_id)
        payment_record_id = payment_status.get("payment_record_id")
        if not payment_record_id:
            return {"handled": False, "error": "no payment record to link"}
        link = payment_service.generate_payment_link(payment_record_id)
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', outcome='payment_link_sent',
                                            current_intent=intent)
        return {"handled": True, "event_type": "payment_link_sent", "link": link}

    def _handle_refused_to_pay(self, intent, entities, context, case_id, customer_id, call_session_id):
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', outcome='refused',
                                            current_intent=intent)
        return {"handled": True, "event_type": "payment_refused"}

    def _handle_financial_hardship(self, intent, entities, context, case_id, customer_id, call_session_id):
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', outcome='follow_up_required',
                                            current_intent=intent)
        return {"handled": True, "event_type": "hardship_recorded", "rag_category": "hardship"}

    def _handle_dispute(self, intent, entities, context, case_id, customer_id, call_session_id):
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', outcome='disputed',
                                            current_intent=intent)
        return {"handled": True, "event_type": "dispute_created", "rag_category": "dispute"}

    def _handle_callback_requested(self, intent, entities, context, case_id, customer_id, call_session_id):
        # Actual Callback row creation belongs to callback_service /
        # callback_tools (they already own that path) — this just
        # updates case state and logs the event.
        if case_id:
            crm_service.update_case_status(case_id, status='in_progress', outcome='callback_scheduled',
                                            current_intent=intent)
        return {"handled": True, "event_type": "callback_requested"}

    def _handle_complaint(self, intent, entities, context, case_id, customer_id, call_session_id):
        if case_id:
            crm_service.mark_complaint(case_id, entities.get("text", ""))
        return {"handled": True, "event_type": "complaint_created", "rag_category": "complaint"}

    def _handle_wrong_number(self, intent, entities, context, case_id, customer_id, call_session_id):
        crm_service.mark_wrong_number(customer_id)
        if case_id:
            crm_service.update_case_status(case_id, status='closed', outcome='wrong_number',
                                            current_intent=intent)
        return {"handled": True, "event_type": "wrong_number", "end_call": True}

    def _handle_account_not_owned(self, intent, entities, context, case_id, customer_id, call_session_id):
        crm_service.mark_account_not_owned(customer_id)
        if case_id:
            crm_service.update_case_status(case_id, status='closed', outcome='account_not_owned',
                                            current_intent=intent)
        return {"handled": True, "event_type": "account_not_owned", "end_call": True}

    def _handle_goodbye(self, intent, entities, context, case_id, customer_id, call_session_id):
        return {"handled": True, "event_type": "call_ended", "end_call": True}

    def _handle_default(self, intent, entities, context, case_id, customer_id, call_session_id):
        return {"handled": True, "event_type": intent}

    _HANDLERS = {
        "payment_done": _handle_payment_done,
        "payment_pending": _handle_payment_pending,
        "promise_to_pay": _handle_promise_to_pay,
        "payment_link_requested": _handle_payment_link,
        "payment_link_resend_requested": _handle_payment_link,
        "refused_to_pay": _handle_refused_to_pay,
        "financial_hardship": _handle_financial_hardship,
        "dispute": _handle_dispute,
        "callback_requested": _handle_callback_requested,
        "complaint": _handle_complaint,
        "wrong_number": _handle_wrong_number,
        "account_not_owned": _handle_account_not_owned,
        "goodbye": _handle_goodbye,
    }

    # ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse_date(value):
        if not value:
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


recovery_service = RecoveryService()