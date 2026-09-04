"""
services/callback_service.py

Owns writes to the Callback model. Per your plan doc (section 15), this
replaces _callbacks_store.json entirely -- nothing should read/write that
file anymore once this is wired in. Delete _callbacks_store.json and grep
the repo for `_callbacks_store` to confirm nothing else touches it.

Fields verified directly against the real models.py Callback/RecoveryEvent
classes (previous version of this file was written against an assumed
schema and doesn't match):

    Callback.recovery_case   -- NOT `case`
    Callback.scheduled_for   -- REQUIRED DateTimeField, NOT requested_date/
                                 requested_time (those fields don't exist)
    Callback.requested_window -- free-text hint (e.g. "shaam ko"), separate
                                  from scheduled_for
    Callback.status choices  -- lowercase: requested / scheduled / completed
                                 / cancelled / missed (NOT "SCHEDULED")
    Callback.session          -- the originating CallSession, NOT `call`
    RecoveryEvent.notes / .payload -- NOT description / metadata
"""

import logging
import re
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

logger = logging.getLogger("recovery_agent")


# ---------------------------------------------------------------------------
# Heuristic free-text -> datetime resolution
# ---------------------------------------------------------------------------
# Callback.scheduled_for is a required DateTimeField, so we cannot leave it
# unset. This is a STOPGAP, not real NLP date resolution -- it recognises a
# small fixed vocabulary of Hindi/Hinglish/English day and time-of-day
# words. Anything it doesn't recognise falls back to "tomorrow, 11:00" with
# confident=False, and the row is saved with status="requested" (needs a
# human to confirm/correct the time) rather than "scheduled". Swap this for
# a real resolver (e.g. `dateparser` with custom Hindi rules) when one is
# available -- the raw phrase is always preserved in requested_window so
# nothing is lost in the meantime.

_DATE_KEYWORDS = {
    "aaj": 0, "today": 0,
    "kal": 1, "tomorrow": 1,
    "parso": 2,
}

_TIME_KEYWORDS = {
    "subah": dt_time(10, 0), "morning": dt_time(10, 0),
    "dopahar": dt_time(14, 0), "afternoon": dt_time(14, 0),
    "shaam": dt_time(18, 0), "evening": dt_time(18, 0),
    "raat": dt_time(21, 0), "night": dt_time(21, 0),
}

_DEFAULT_DAY_OFFSET = 1        # tomorrow
_DEFAULT_TIME = dt_time(11, 0)  # 11:00


def _heuristic_resolve(requested_window: Optional[str], now=None):
    """Return (scheduled_for: aware datetime, confident: bool)."""
    from django.utils import timezone

    now = now or timezone.now()
    text = (requested_window or "").lower()
    words = re.findall(r"[a-zA-Z\u0900-\u097F]+", text)

    day_offset = None
    time_of_day = None
    for w in words:
        if w in _DATE_KEYWORDS and day_offset is None:
            day_offset = _DATE_KEYWORDS[w]
        if w in _TIME_KEYWORDS and time_of_day is None:
            time_of_day = _TIME_KEYWORDS[w]

    confident = day_offset is not None or time_of_day is not None

    target_date = (now + timedelta(days=day_offset if day_offset is not None else _DEFAULT_DAY_OFFSET)).date()
    naive_dt = datetime.combine(target_date, time_of_day or _DEFAULT_TIME)
    scheduled_for = timezone.make_aware(naive_dt) if timezone.is_naive(naive_dt) else naive_dt

    return scheduled_for, confident


class CallbackService:
    def schedule_callback(
        self,
        customer_id,
        requested_window: Optional[str] = None,
        reason: str = "customer_requested",
        session_id: Optional[str] = None,
    ) -> dict:
        from recovery_agent.models import Callback, RecoveryCase, RecoveryEvent, CallSession

        call = None
        case = None
        if session_id:
            call = CallSession.objects.filter(session_id=session_id).order_by("-id").first()
            if call:
                case = getattr(call, "recovery_case", None)
        if not case:
            case = RecoveryCase.objects.filter(customer_id=customer_id).order_by("-id").first()

        scheduled_for, confident = _heuristic_resolve(requested_window)
        status = "scheduled" if confident else "requested"

        callback = Callback.objects.create(
            customer_id=customer_id,
            recovery_case=case,
            session=call,
            scheduled_for=scheduled_for,
            requested_window=(requested_window or "")[:100],
            reason=reason,
            status=status,
        )

        if case:
            RecoveryEvent.objects.create(
                case=case,
                event_type="callback_scheduled" if confident else "callback_requested",
                intent="callback_requested",
                call_session=call,
                payload={
                    "callback_id": callback.pk,
                    "scheduled_for": scheduled_for.isoformat(),
                    "requested_window": requested_window,
                    "confident": confident,
                },
                notes=reason,
            )
        else:
            logger.warning(
                "schedule_callback: no RecoveryCase found for customer_id=%s "
                "(callback saved, but no RecoveryEvent recorded)",
                customer_id,
            )

        return {
            "scheduled": True,
            "callback_id": callback.pk,
            "scheduled_for": scheduled_for.isoformat(),
            "requested_window": requested_window,
            "status": status,
            "confident": confident,
        }


callback_service = CallbackService()