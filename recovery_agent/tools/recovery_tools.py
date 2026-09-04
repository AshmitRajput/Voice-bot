"""
recovery_tools.py

Core recovery-domain tool implementations for the Recovery Agent.

These are the actual DB-touching functions behind the LLM-facing tools
declared in recovery_tools_defs.py, callback_tools_defs.py, and
call_control_tools_defs.py. No tool-JSON / prompt-parsing logic lives here
-- that's in tool_registry.py and *_defs.py. Written directly against your
real recovery_agent/models.py (13-model schema), so no more "# ASSUMES".

FK names to remember (these are the ones that don't match the plan doc's
prose and would silently break if you go by memory):
    PaymentRecord.recovery_case  -- NOT `case`
    Callback.recovery_case       -- NOT `case`
    Callback.session             -- NOT `call`
    RecoveryEvent.case           -- yes, this one really is `case`
    RecoveryEvent.payload / .notes / .occurred_at  -- NOT metadata/description/created_at
"""

import logging
from typing import Optional

logger = logging.getLogger("recovery_agent")

_VALID_PROMISE = {"yes", "no", "unclear"}


# ---------------------------------------------------------------------------
# session_id / case_id -> RecoveryCase resolution
# ---------------------------------------------------------------------------

def _resolve_case(session_id: Optional[str] = None, case_id=None):
    """Priority: explicit case_id > CallSession.recovery_case (via session_id)."""
    from recovery_agent.models import RecoveryCase, CallSession

    if case_id:
        return (
            RecoveryCase.objects.filter(pk=case_id)
            .select_related("customer")
            .first()
        )

    if session_id:
        call = (
            CallSession.objects.filter(session_id=session_id)
            .select_related("recovery_case", "recovery_case__customer")
            .order_by("-id")
            .first()
        )
        if call and call.recovery_case_id:
            return call.recovery_case

    return None


def _resolve_call(session_id: Optional[str] = None):
    from recovery_agent.models import CallSession
    if not session_id:
        return None
    return CallSession.objects.filter(session_id=session_id).order_by("-id").first()


# ---------------------------------------------------------------------------
# get_recovery_context -- READ ONLY
# ---------------------------------------------------------------------------

def get_recovery_context(session_id: Optional[str] = None, case_id=None) -> dict:
    case = _resolve_case(session_id=session_id, case_id=case_id)
    if not case:
        return {"success": False, "error": "no_active_recovery_case"}

    customer = case.customer
    # PaymentRecord.recovery_case has related_name='payments'
    payment = case.payments.order_by("-id").first()
    # RecoveryEvent.case has related_name='events'
    recent_events = list(
        case.events.order_by("-occurred_at").values(
            "event_type", "intent", "notes", "occurred_at"
        )[:5]
    )

    return {
        "success": True,
        "case_id": case.pk,
        "customer_name": customer.name or None,
        "phone_number": customer.phone_number,
        "do_not_call": customer.do_not_call,
        "case_type": case.case_type,
        "amount_due": str(case.amount_due),
        "amount_recovered": str(case.amount_recovered),
        "outstanding_amount": str(payment.outstanding_amount) if payment else str(case.amount_due),
        "currency": case.currency,
        "due_date": str(case.due_date) if case.due_date else None,
        "status": case.status,
        "priority": case.priority,
        "case_outcome": case.outcome or None,
        "current_intent": case.current_intent or None,
        "current_outcome": case.current_outcome or None,
        "promise_date": str(case.promise_date) if case.promise_date else None,
        "payment_status": payment.status if payment else None,
        "payment_short_url": payment.short_url if payment else None,
        "last_contacted_at": case.last_contacted_at.isoformat() if case.last_contacted_at else None,
        "recent_events": [
            {**e, "occurred_at": e["occurred_at"].isoformat() if e["occurred_at"] else None}
            for e in recent_events
        ],
    }


# ---------------------------------------------------------------------------
# update_recovery_case -- WRITE (conversation-derived fields only)
# ---------------------------------------------------------------------------

def update_recovery_case(
    session_id: Optional[str] = None,
    case_id=None,
    customer_intent: Optional[str] = None,
    promise_to_pay: Optional[str] = None,
    promise_date: Optional[str] = None,
    recovery_reason: Optional[str] = None,
    preferred_channel: Optional[str] = None,
    sentiment: Optional[str] = None,
    next_action: Optional[str] = None,
    notes: Optional[str] = None,
    **_ignored,
) -> dict:
    from django.utils import timezone

    case = _resolve_case(session_id=session_id, case_id=case_id)
    if not case:
        return {"success": False, "error": "no_active_recovery_case"}

    if promise_to_pay is not None and promise_to_pay not in _VALID_PROMISE:
        return {"success": False, "error": f"invalid promise_to_pay: {promise_to_pay!r}"}

    if promise_date and promise_to_pay != "yes":
        return {"success": False, "error": "promise_date supplied without promise_to_pay='yes'"}

    changed = set()

    if customer_intent:
        case.current_intent = customer_intent
        changed.add("current_intent")

    # RecoveryCase.status has real choices: open / in_progress / closed / reopened.
    # "promise_recorded" etc. are NOT valid status values -- they belong in
    # current_outcome (free text, no choices constraint).
    if case.status == "open":
        case.status = "in_progress"
        changed.add("status")

    if promise_to_pay == "yes":
        case.current_outcome = "promise_recorded"
        changed.add("current_outcome")
        if promise_date:
            case.promise_date = promise_date
            changed.add("promise_date")
    elif promise_to_pay == "no":
        case.current_outcome = "refused"
        changed.add("current_outcome")

    case.last_contacted_at = timezone.now()
    changed.add("last_contacted_at")

    case.save(update_fields=list(changed))

    call = _resolve_call(session_id)
    case.events.create(
        event_type="recovery_case_updated",
        intent=customer_intent or "",
        call_session=call,
        payload={
            "promise_to_pay": promise_to_pay,
            "preferred_channel": preferred_channel,
            "sentiment": sentiment,
            "next_action": next_action,
        },
        notes=notes or recovery_reason or "",
    )

    return {
        "success": True,
        "case_id": case.pk,
        "status": case.status,
        "current_outcome": case.current_outcome,
        "promise_date": str(case.promise_date) if case.promise_date else None,
    }


# ---------------------------------------------------------------------------
# create_payment_link
# ---------------------------------------------------------------------------

def create_payment_link(session_id: Optional[str] = None, case_id=None, channel: str = "whatsapp") -> dict:
    from recovery_agent.models import PaymentRecord

    if channel not in {"whatsapp", "sms", "email"}:
        return {"success": False, "error": f"invalid channel: {channel!r}"}

    case = _resolve_case(session_id=session_id, case_id=case_id)
    if not case:
        return {"success": False, "error": "no_active_recovery_case"}

    call = _resolve_call(session_id)

    payment = case.payments.order_by("-id").first()
    if payment is None or payment.status in ("failed",):
        payment = PaymentRecord.objects.create(
            customer=case.customer,
            recovery_case=case,
            session=call,
            amount_due=case.amount_due,
            amount_paid=0,
            outstanding_amount=case.amount_due - case.amount_recovered,
            currency=case.currency,
            status="pending",
            provider="manual",
        )

    from recovery_agent.services.payment_service import payment_service
    link = payment_service.generate_payment_link(payment_id=payment.pk, channel=channel)

    payment.short_url = link
    if payment.status == "pending":
        payment.status = "link_sent"
    payment.save(update_fields=["short_url", "status"])

    payment.events.create(
        event_type="payment_link_created",
        provider_event_id="",
        amount=payment.outstanding_amount,
        payload={"channel": channel},
    )

    return {
        "success": True,
        "payment_id": payment.pk,
        "channel": channel,
        "payment_link": link,
        "message": f"Payment link bhej diya hai aapke {channel} par.",
    }


# ---------------------------------------------------------------------------
# record_call_completion -- infrastructure fallback used by end_call
# ---------------------------------------------------------------------------

# Reasons from call_control_tools_defs.py that mean "this case is settled,
# stop calling" -- closes the RecoveryCase and mirrors the reason into its
# free-text `outcome` field. Everything else just logs a RecoveryEvent
# without closing the case.
_CASE_CLOSING_REASONS = {
    "declined": "declined",
    "do_not_call": "do_not_call",
    "wrong_number": "wrong_number",
}


def record_call_completion(session_id: Optional[str] = None, reason: Optional[str] = None) -> dict:
    from django.utils import timezone

    case = _resolve_case(session_id=session_id)
    call = _resolve_call(session_id)

    if call:
        call.status = "completed"
        call.ended_at = timezone.now()
        call.recovery_outcome = reason or "completed"
        update_fields = ["status", "ended_at", "recovery_outcome"]
        if call.started_at and not call.duration_seconds:
            call.duration_seconds = int((call.ended_at - call.started_at).total_seconds())
            update_fields.append("duration_seconds")
        call.save(update_fields=update_fields)

    if not case:
        logger.warning("record_call_completion: no recovery case for session %s", session_id)
        return {"success": False, "error": "no_active_recovery_case", "reason": reason}

    if reason in ("wrong_number",):
        # do_not_call is on Customer too -- wrong number means we should
        # stop calling this phone number entirely, not just close one case.
        case.customer.do_not_call = True
        case.customer.do_not_call_reason = "wrong_number"
        case.customer.save(update_fields=["do_not_call", "do_not_call_reason"])
    elif reason == "do_not_call":
        case.customer.do_not_call = True
        case.customer.do_not_call_reason = "customer_requested"
        case.customer.save(update_fields=["do_not_call", "do_not_call_reason"])

    if reason in _CASE_CLOSING_REASONS:
        case.status = "closed"
        case.outcome = _CASE_CLOSING_REASONS[reason]
        case.closed_at = timezone.now()
        case.save(update_fields=["status", "outcome", "closed_at"])

    case.events.create(
        event_type="call_completed",
        intent="",
        call_session=call,
        payload={"reason": reason},
        notes=f"Call ended: {reason or 'completed'}",
    )

    return {"success": True, "case_id": case.pk, "reason": reason, "case_status": case.status}


# ---------------------------------------------------------------------------
# register_all_recovery_tools -- called once by RecoveryService.__init__
# ---------------------------------------------------------------------------

_registered_once = False


def register_all_recovery_tools():
    """Idempotent. Importing recovery_agent.tools triggers the three
    *_defs.py modules' own import-time ToolSpec registration."""
    global _registered_once
    if _registered_once:
        return
    import recovery_agent.tools  # noqa: F401
    _registered_once = True