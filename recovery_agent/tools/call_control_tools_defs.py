"""
call_control_tools_defs.py

BharatRouter-facing glue for call control in the Recovery Agent.

end_call is a terminal tool. It also performs a small server-side recovery
call-completion write so a missing update_recovery_case call does not leave the
case completely without a call-completed audit event.
"""

import logging
import threading

from recovery_agent.tools.tool_registry import (
    ToolSpec,
    register_tool,
    get_tool_session_id,
    mark_call_for_ending,
)
from recovery_agent.tools.recovery_tools import record_call_completion

logger = logging.getLogger("voice_bot")


END_CALL_PROMPT_BLOCK = """
- end_call:

  - Use this ONLY when the customer has clearly indicated that they want the
    current call to end.
  - A natural pause or a resolved payment question does NOT by itself mean
    the customer wants to end the call.
  - If the conversation is complete but the customer has not clearly ended it,
    first ask whether you may end the call and wait for the response.
  - If meaningful recovery information was learned during the call, call
    update_recovery_case BEFORE end_call whenever possible.
  - It is valid for a customer to make NO payment promise. Never invent one
    merely to complete the database update.
  - If the customer clearly says they do not want to pay, use reason
    "declined". If they explicitly ask not to be contacted, use "do_not_call".
  - Valid reasons are:
      customer_goodbye
      declined
      do_not_call
      wrong_number
      angry_customer
      callback
      completed
      other
  - First compose the exact concise Hindi/Hinglish closing line.
  - The ENTIRE reply for this tool turn must be ONLY:
    {"tool": "end_call", "arguments": {"reason": "<reason>", "closing_message": "<line>"}}
  - Never claim that payment succeeded merely because the call ended.
""".strip()


_VALID_REASONS = {
    "customer_goodbye",
    "declined",
    "do_not_call",
    "wrong_number",
    "angry_customer",
    "callback",
    "completed",
    "other",
}

_DEFAULT_CLOSING = "धन्यवाद, आपका दिन शुभ हो। नमस्ते।"


def _end_call(args: dict) -> dict:
    session_id = args.get("session_id") or get_tool_session_id()
    reason = (args.get("reason") or "other").strip()
    closing_message = (args.get("closing_message") or "").strip()

    if reason not in _VALID_REASONS:
        logger.warning(
            "end_call: unrecognised reason %r, defaulting to other",
            reason,
        )
        reason = "other"

    if not closing_message:
        closing_message = _DEFAULT_CLOSING

    if not session_id:
        logger.error(
            "end_call: no active tool session -- cannot mark call for ending"
        )
        return {
            "success": False,
            "error": "no_active_session",
            "message": closing_message,
        }

    # Infrastructure fallback: if the LLM forgot to call
    # update_recovery_case, the case still gets a call_completed audit event.
    completion_result = record_call_completion(
        session_id=session_id,
        reason=reason,
    )

    mark_call_for_ending(session_id, reason)

    return {
        "success": True,
        "reason": reason,
        "message": closing_message,
        "recovery_call_recorded": completion_result,
    }


_register_lock = threading.Lock()
_registered = False


def _register_call_control_tools():
    global _registered
    if _registered:
        return

    with _register_lock:
        if _registered:
            return

        register_tool(
            ToolSpec(
                name="end_call",
                description=(
                    "End the current recovery call only after the customer has "
                    "clearly indicated that they want the call to end. "
                    "Records a call-completed audit event and disconnects."
                ),
                parameters={
                    "reason": {
                        "type": "string",
                        "description": (
                            "One of: customer_goodbye, declined, do_not_call, "
                            "wrong_number, angry_customer, callback, completed, other."
                        ),
                    },
                    "closing_message": {
                        "type": "string",
                        "description": (
                            "Exact natural Hindi/Hinglish closing line. "
                            "Spoken verbatim before disconnecting."
                        ),
                    },
                },
                impl=_end_call,
                prompt_block=END_CALL_PROMPT_BLOCK,
                terminal=True,
            ),
            override=True,
        )

        _registered = True
        logger.info("call_control_tools_defs: registered end_call")


_register_call_control_tools()
