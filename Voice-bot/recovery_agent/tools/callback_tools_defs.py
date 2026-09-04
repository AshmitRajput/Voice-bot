"""
callback_tools_defs.py

BharatRouter-facing glue for scheduling a recovery callback.

schedule_callback is a terminal conversational action: it records the
customer's request and marks the current call for ending. It is NOT used just
because a recovery conversation reached a natural pause.
"""

import logging
import threading

from recovery_agent.tools.tool_registry import (
    ToolSpec,
    register_tool,
    get_tool_session_id,
    get_call_context,
    mark_call_for_ending,
)
from recovery_agent.tools.callback_tools import schedule_callback
from recovery_agent.tools.recovery_tools import update_recovery_case, record_call_completion

logger = logging.getLogger("voice_bot")


SCHEDULE_CALLBACK_PROMPT_BLOCK = """
- schedule_callback:
  - Use this ONLY when the customer genuinely asks to be called back later
    instead of continuing the current recovery conversation.
  - Valid examples include:
    * customer is busy
    * customer wants to talk later
    * customer explicitly asks for a callback
    * customer gives a preferred callback day/time
  - Do NOT use it merely because:
    * the customer said "ठीक है"
    * the customer said "धन्यवाद"
    * the payment issue was discussed
    * the conversation reached a natural pause
  - preferred_time is optional. Never invent a callback date/time.
  - If the customer did not give a date/time, omit preferred_time.
  - schedule_callback records the callback and ends the current call.
  - Do NOT call end_call after schedule_callback succeeds.
  - Before calling this tool, if meaningful recovery information was learned,
    call update_recovery_case first.
  - The ENTIRE tool-call turn must be ONLY:
    {"tool": "schedule_callback", "arguments": {"closing_message": "<line>", "preferred_time": "<optional>"}}
""".strip()


_DEFAULT_CLOSING = (
    "ठीक है जी, कोई बात नहीं। मैं आपको बाद में कॉलबैक कर लूंगी। "
    "धन्यवाद, नमस्ते।"
)


def _schedule_callback(args: dict) -> dict:
    session_id = args.get("session_id") or get_tool_session_id()
    closing_message = (args.get("closing_message") or "").strip()
    preferred_time = (args.get("preferred_time") or "").strip() or None

    if not closing_message:
        logger.warning(
            "schedule_callback: no closing_message supplied, using default"
        )
        closing_message = _DEFAULT_CLOSING

    if not session_id:
        logger.error(
            "schedule_callback: no active tool session -- cannot schedule"
        )
        return {
            "success": False,
            "error": "no_active_session",
            "message": closing_message,
        }

    ctx = get_call_context(session_id)
    phone_number = ctx.get("phone_number")
    customer_name = ctx.get("customer_name") or "Customer"

    try:
        callback_result = schedule_callback(
            session_id=session_id,
            phone_number=phone_number,
            customer_name=customer_name,
            reason=preferred_time or "customer requested callback",
            requested_for=preferred_time,
        )

        # This is a server-side DB update, not an additional LLM tool.
        # It records that the recovery case now waits for a callback.
        case_update = update_recovery_case(
            session_id=session_id,
            next_action="follow_up",
            preferred_channel="voice",
            notes=(
                f"Customer requested callback"
                + (f" for {preferred_time}" if preferred_time else "")
            ),
        )

        # Infrastructure fallback, same as _end_call: the callback path is
        # a valid way for a call to finish, but nothing was previously
        # calling record_call_completion() here -- only mark_call_for_ending
        # was called. That left the CallSession row stuck at
        # status="ongoing" forever, with no ended_at/duration_seconds, for
        # every call that ended via callback. record_call_completion()
        # closes out the CallSession the same way _end_call does; reason
        # "callback" is not in _CASE_CLOSING_REASONS so it will NOT close
        # the RecoveryCase itself -- only end_call's non-callback reasons do.
        completion_result = record_call_completion(
            session_id=session_id,
            reason="callback",
        )

        mark_call_for_ending(session_id, "callback")

    except Exception as e:
        logger.exception("schedule_callback: write failed")
        return {
            "success": False,
            "error": str(e),
            "message": closing_message,
        }

    return {
        "success": True,
        "reason": "callback",
        "scheduled": callback_result,
        "case_update": case_update,
        "recovery_call_recorded": completion_result,
        "message": closing_message,
    }


_register_lock = threading.Lock()
_registered = False


def _register_callback_tools():
    global _registered
    if _registered:
        return

    with _register_lock:
        if _registered:
            return

        register_tool(
            ToolSpec(
                name="schedule_callback",
                description=(
                    "Schedule a callback only when the customer explicitly "
                    "wants to continue the recovery conversation later. "
                    "Records the callback and ends the current call."
                ),
                parameters={
                    "closing_message": {
                        "type": "string",
                        "description": (
                            "Exact natural Hindi/Hinglish closing line to speak "
                            "before the call ends."
                        ),
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": (
                            "Optional. The callback day/time exactly as stated "
                            "by the customer. Omit if unspecified."
                        ),
                    },
                },
                impl=_schedule_callback,
                prompt_block=SCHEDULE_CALLBACK_PROMPT_BLOCK,
                terminal=True,
            ),
            override=True,
        )
        _registered = True
        logger.info("callback_tools_defs: registered schedule_callback")


_register_callback_tools()