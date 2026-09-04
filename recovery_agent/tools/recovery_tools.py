"""
recovery_tools.py

Recovery-domain helpers used by the LLM tool layer.

This is intentionally file-backed for the first buildathon version so the
same tool layer can be tested without requiring the Django ORM to be wired in.
The JSON files act as the local development DB. Later, the read/write helpers
can be replaced by Django ORM services without changing recovery_tools_defs.py.

LLM-facing recovery operations:
    1. get_recovery_context()
    2. update_recovery_case()
    3. create_payment_link()

IMPORTANT:
    Payment success/failure is NOT written by these LLM tools. A verified
    payment-provider webhook must be the source of truth for payment status.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from typing import Any, Optional


TOOLS_DIR = os.path.dirname(__file__)
CASES_FILE = os.path.join(TOOLS_DIR, "_recovery_cases_store.json")
EVENTS_FILE = os.path.join(TOOLS_DIR, "_recovery_events_store.json")
PAYMENT_LINKS_FILE = os.path.join(TOOLS_DIR, "_payment_links_store.json")

_lock = threading.RLock()

VALID_CASE_STATUSES = {
    "detected",
    "diagnosed",
    "contact_pending",
    "contacted",
    "promise_to_pay",
    "payment_pending",
    "follow_up",
    "recovered",
    "declined",
    "closed",
}

VALID_PAYMENT_STATUSES = {
    "pending",
    "failed",
    "overdue",
    "successful",
    "cancelled",
}

VALID_INTENTS = {
    "willing_to_pay",
    "not_willing_to_pay",
    "needs_more_time",
    "payment_problem",
    "already_paid",
    "disputed",
    "unclear",
    "unknown",
}

VALID_PROMISE_STATES = {"yes", "no", "unclear"}

VALID_NEXT_ACTIONS = {
    "none",
    "retry_payment",
    "send_payment_link",
    "voice_call",
    "whatsapp",
    "follow_up",
    "human_escalation",
    "wait_for_payment",
    "close_case",
}


# ---------------------------------------------------------------------------
# JSON persistence helpers
# ---------------------------------------------------------------------------


def _load_list(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_list(path: str, records: list[dict[str, Any]]) -> None:
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Case lookup helpers
# ---------------------------------------------------------------------------


def _find_case(
    cases: list[dict[str, Any]],
    *,
    case_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if case_id:
        for case in cases:
            if str(case.get("case_id")) == str(case_id):
                return case

    if session_id:
        # Prefer an active case. If none is active, fall back to the latest
        # case belonging to the session.
        session_cases = [
            c for c in cases if str(c.get("session_id")) == str(session_id)
        ]
        if session_cases:
            active = [
                c
                for c in session_cases
                if c.get("status") not in {"recovered", "closed"}
            ]
            return (active or session_cases)[-1]

    return None


def create_recovery_case(
    *,
    customer_id: str,
    customer_name: str,
    phone_number: Optional[str],
    amount: float,
    case_type: str,
    payment_status: str = "failed",
    failure_reason: Optional[str] = None,
    risk: str = "medium",
    session_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Backend helper for creating a recovery case before a call starts.

    This is intentionally NOT registered as an LLM tool. Cases should be
    created by ingestion/webhook/backend code, not by the conversational LLM.
    """
    if payment_status not in VALID_PAYMENT_STATUSES:
        raise ValueError(f"Invalid payment_status: {payment_status}")

    now = _now()
    case = {
        "case_id": _new_id("rec"),
        "session_id": str(session_id) if session_id else None,
        "customer_id": str(customer_id),
        "customer_name": customer_name or "Customer",
        "phone_number": phone_number,
        "amount": float(amount),
        "outstanding_amount": float(amount),
        "case_type": case_type,
        "status": "detected",
        "payment_status": payment_status,
        "failure_reason": failure_reason,
        "risk": risk,
        "customer_intent": "unknown",
        "recovery_reason": None,
        "promise_to_pay": "unclear",
        "promise_date": None,
        "preferred_channel": None,
        "sentiment": None,
        "next_action": "voice_call",
        "last_action": None,
        "attempt_count": 0,
        "last_contacted_at": None,
        "next_action_at": None,
        "payment_link_id": None,
        "payment_link": None,
        "recovered_amount": 0.0,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
    }

    with _lock:
        cases = _load_list(CASES_FILE)
        cases.append(case)
        _save_list(CASES_FILE, cases)
        _append_event_locked(
            case["case_id"],
            "case_detected",
            {"payment_status": payment_status, "amount": amount},
        )

    return dict(case)


# ---------------------------------------------------------------------------
# Event/audit helpers
# ---------------------------------------------------------------------------


def _append_event_locked(
    case_id: str,
    event_type: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    events = _load_list(EVENTS_FILE)
    event = {
        "event_id": _new_id("evt"),
        "case_id": case_id,
        "event_type": event_type,
        "metadata": metadata or {},
        "created_at": _now(),
    }
    events.append(event)
    _save_list(EVENTS_FILE, events)
    return event


def record_call_completion(
    session_id: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Backend/infrastructure fallback used by end_call.

    It guarantees that ending a recovery call creates an audit event even if
    the LLM forgot to call update_recovery_case(). It deliberately does not
    invent customer intent or a promise.
    """
    if not session_id:
        return {"success": False, "error": "no_active_session"}

    with _lock:
        cases = _load_list(CASES_FILE)
        case = _find_case(cases, session_id=session_id)
        if not case:
            return {"success": False, "error": "recovery_case_not_found"}

        now = _now()
        case["status"] = (
            "contacted"
            if case.get("status") not in {"recovered", "closed"}
            else case["status"]
        )
        case["last_contacted_at"] = now
        case["updated_at"] = now
        case["attempt_count"] = int(case.get("attempt_count") or 0) + 1

        event = _append_event_locked(
            case["case_id"],
            "call_completed",
            {"reason": reason or "unknown"},
        )
        _save_list(CASES_FILE, cases)

        return {
            "success": True,
            "case_id": case["case_id"],
            "status": case["status"],
            "attempt_count": case["attempt_count"],
            "event_id": event["event_id"],
        }


# ---------------------------------------------------------------------------
# LLM tool 1: context/read
# ---------------------------------------------------------------------------


def get_recovery_context(
    session_id: Optional[str] = None,
    case_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return the current recovery state needed by the conversation."""
    if not session_id and not case_id:
        return {"success": False, "error": "no_session_or_case_id"}

    with _lock:
        cases = _load_list(CASES_FILE)
        case = _find_case(cases, case_id=case_id, session_id=session_id)

        if not case:
            return {
                "success": False,
                "error": "recovery_case_not_found",
                "message": "No recovery case is attached to this call session.",
            }

        events = _load_list(EVENTS_FILE)
        case_events = [
            event for event in events if event.get("case_id") == case["case_id"]
        ]

        # Keep the LLM context compact: latest 10 audit events only.
        recent_events = case_events[-10:]

        return {
            "success": True,
            "case": dict(case),
            "recent_events": recent_events,
        }


# ---------------------------------------------------------------------------
# LLM tool 2: structured conversation outcome/write
# ---------------------------------------------------------------------------


def update_recovery_case(
    *,
    session_id: Optional[str] = None,
    case_id: Optional[str] = None,
    customer_intent: Optional[str] = None,
    promise_to_pay: Optional[str] = None,
    promise_date: Optional[str] = None,
    recovery_reason: Optional[str] = None,
    preferred_channel: Optional[str] = None,
    sentiment: Optional[str] = None,
    next_action: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Safely update structured information extracted from the conversation.

    Promise fields are intentionally optional.

    Examples:
        Customer promises:
            promise_to_pay="yes", promise_date="2026-09-07"

        Customer does not promise:
            promise_to_pay="no"

        Model cannot determine whether there was a promise:
            promise_to_pay="unclear"

    A promise date is NEVER accepted unless promise_to_pay == "yes".
    """
    if not session_id and not case_id:
        return {"success": False, "error": "no_session_or_case_id"}

    if customer_intent is not None and customer_intent not in VALID_INTENTS:
        return {
            "success": False,
            "error": "invalid_customer_intent",
            "allowed": sorted(VALID_INTENTS),
        }

    if promise_to_pay is not None and promise_to_pay not in VALID_PROMISE_STATES:
        return {
            "success": False,
            "error": "invalid_promise_to_pay",
            "allowed": sorted(VALID_PROMISE_STATES),
        }

    if next_action is not None and next_action not in VALID_NEXT_ACTIONS:
        return {
            "success": False,
            "error": "invalid_next_action",
            "allowed": sorted(VALID_NEXT_ACTIONS),
        }

    if promise_date:
        try:
            dt.date.fromisoformat(promise_date)
        except ValueError:
            return {
                "success": False,
                "error": "invalid_promise_date",
                "message": "promise_date must use YYYY-MM-DD format.",
            }

    if promise_date and promise_to_pay != "yes":
        return {
            "success": False,
            "error": "promise_date_requires_confirmed_promise",
            "message": (
                "Only provide promise_date when the customer clearly committed "
                "to paying and promise_to_pay is exactly 'yes'."
            ),
        }

    fields = {
        "customer_intent": customer_intent,
        "promise_to_pay": promise_to_pay,
        "promise_date": promise_date,
        "recovery_reason": recovery_reason,
        "preferred_channel": preferred_channel,
        "sentiment": sentiment,
        "next_action": next_action,
        "notes": notes,
    }
    supplied_fields = {k: v for k, v in fields.items() if v is not None}

    if not supplied_fields:
        return {
            "success": False,
            "error": "no_update_fields",
            "message": "Provide at least one recovery outcome field.",
        }

    with _lock:
        cases = _load_list(CASES_FILE)
        case = _find_case(cases, case_id=case_id, session_id=session_id)

        if not case:
            return {
                "success": False,
                "error": "recovery_case_not_found",
            }

        # Never allow this conversational tool to modify payment truth.
        # payment_status, amount and outstanding_amount are intentionally not
        # accepted as arguments.
        now = _now()

        if promise_to_pay == "yes":
            case["status"] = "promise_to_pay"
        elif promise_to_pay == "no":
            case["promise_date"] = None
            if case.get("status") == "promise_to_pay":
                case["status"] = "contacted"

        # If the model explicitly says the promise is unclear, keep the date
        # absent rather than guessing from a vague statement.
        if promise_to_pay == "unclear":
            case["promise_date"] = None

        for field, value in supplied_fields.items():
            if field == "promise_date":
                # Already validated above.
                case[field] = value
            elif field == "notes":
                case[field] = value
            else:
                case[field] = value

        if next_action is not None:
            case["last_action"] = next_action

        case["updated_at"] = now

        event_metadata = {
            "updated_fields": supplied_fields,
            "promise_to_pay": case.get("promise_to_pay"),
            "promise_date": case.get("promise_date"),
        }

        event_type = (
            "promise_to_pay_created"
            if promise_to_pay == "yes"
            else "recovery_case_updated"
        )
        event = _append_event_locked(case["case_id"], event_type, event_metadata)
        _save_list(CASES_FILE, cases)

        return {
            "success": True,
            "case_id": case["case_id"],
            "status": case["status"],
            "updated_fields": list(supplied_fields.keys()),
            "customer_intent": case.get("customer_intent"),
            "promise_to_pay": case.get("promise_to_pay"),
            "promise_date": case.get("promise_date"),
            "next_action": case.get("next_action"),
            "event_id": event["event_id"],
        }


# ---------------------------------------------------------------------------
# LLM tool 3: payment-link request
# ---------------------------------------------------------------------------


def create_payment_link(
    *,
    session_id: Optional[str] = None,
    case_id: Optional[str] = None,
    channel: str = "whatsapp",
) -> dict[str, Any]:
    """Create a development payment-link record for the active recovery case.

    The actual Razorpay integration can replace this helper later. This first
    version deliberately creates a local link record instead of pretending a
    real payment was completed.
    """
    if not session_id and not case_id:
        return {"success": False, "error": "no_session_or_case_id"}

    channel = (channel or "whatsapp").strip().lower()
    if channel not in {"whatsapp", "sms", "email"}:
        return {
            "success": False,
            "error": "invalid_channel",
            "allowed": ["whatsapp", "sms", "email"],
        }

    with _lock:
        cases = _load_list(CASES_FILE)
        case = _find_case(cases, case_id=case_id, session_id=session_id)

        if not case:
            return {"success": False, "error": "recovery_case_not_found"}

        if case.get("payment_status") == "successful" or case.get("status") == "recovered":
            return {
                "success": False,
                "error": "payment_already_successful",
                "message": "Do not create a payment link for a recovered case.",
            }

        amount = float(case.get("outstanding_amount") or case.get("amount") or 0)
        if amount <= 0:
            return {"success": False, "error": "no_outstanding_amount"}

        link_id = _new_id("plink")
        # Development-only URL. Replace this with the Razorpay short_url when
        # the payment provider client is connected.
        payment_link = f"http://localhost:8000/pay/{link_id}"
        now = _now()

        link_record = {
            "payment_link_id": link_id,
            "case_id": case["case_id"],
            "amount": amount,
            "channel": channel,
            "status": "created",
            "payment_link": payment_link,
            "created_at": now,
        }

        links = _load_list(PAYMENT_LINKS_FILE)
        links.append(link_record)
        _save_list(PAYMENT_LINKS_FILE, links)

        case["payment_link_id"] = link_id
        case["payment_link"] = payment_link
        case["next_action"] = "wait_for_payment"
        case["last_action"] = "send_payment_link"
        case["status"] = "payment_pending"
        case["updated_at"] = now

        event = _append_event_locked(
            case["case_id"],
            "payment_link_created",
            {
                "payment_link_id": link_id,
                "amount": amount,
                "channel": channel,
            },
        )
        _save_list(CASES_FILE, cases)

        return {
            "success": True,
            "case_id": case["case_id"],
            "payment_link_id": link_id,
            "payment_link": payment_link,
            "amount": amount,
            "channel": channel,
            "event_id": event["event_id"],
            "payment_status": case["payment_status"],
            "message": (
                "Payment link created. This does not mean the payment is "
                "successful; wait for the verified payment webhook."
            ),
        }
