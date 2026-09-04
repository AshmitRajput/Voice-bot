"""
recovery_tools_defs.py

BharatRouter-facing tool definitions for the Recovery Agent conversation.

Three recovery-domain tools are exposed to the LLM:
    - get_recovery_context
    - update_recovery_case
    - create_payment_link

Call-control (end_call) and callback scheduling remain separate infrastructure
concerns and are registered by their own modules.
"""

import logging
import threading

from recovery_agent.tools.tool_registry import (
    ToolSpec,
    register_tool,
    get_tool_session_id,
)
from recovery_agent.tools.recovery_tools import (
    get_recovery_context,
    update_recovery_case,
    create_payment_link,
)

logger = logging.getLogger("voice_bot")


RECOVERY_PROMPT_BLOCK = """
RECOVERY AGENT TOOL RULES

- get_recovery_context:
  - Use this at the beginning of a recovery call, before making claims about
    the customer's payment, amount, previous attempts, or recovery state.
  - This is READ-ONLY.
  - Never invent a recovery case, payment status, amount, customer history,
    promise, or previous action.

- update_recovery_case:
  - Use this after the customer provides meaningful information about why
    they did not pay, whether they intend to pay, whether they made a promise,
    or what follow-up/action is appropriate.
  - All update fields are OPTIONAL. Do NOT invent missing information.
  - customer_intent must describe what the customer actually communicated.
  - promise_to_pay MUST be one of:
      "yes"      = customer clearly committed to paying
      "no"       = customer clearly did not commit / declined
      "unclear"  = the conversation does not establish a promise
  - promise_date is OPTIONAL and must ONLY be supplied when
    promise_to_pay is exactly "yes" and the customer clearly gave a date.
  - If the customer says something vague such as "dekhenge", "shayad",
    "jaldi karunga", or otherwise does not give a clear commitment, use
    promise_to_pay="unclear" and DO NOT invent a promise_date.
  - If the customer explicitly says they will not pay, use promise_to_pay="no".
  - If there is no promise at all, use promise_to_pay="no" only when the
    customer clearly declined/non-committed; otherwise use "unclear".
  - If the LLM is unsure how to classify the statement, prefer "unclear"
    instead of guessing.
  - If the tool returns an error, correct the arguments and retry. Do not tell
    the customer that the database was updated unless the tool returned
    success=true.
  - NEVER pass payment_status, amount, outstanding_amount, recovered_amount,
    or any field that claims payment success. Those values are controlled by
    backend/payment-provider events.

- create_payment_link:
  - Use only when a payment link is actually appropriate for the conversation
    and the customer has agreed to receive one, or the approved recovery
    workflow explicitly requires sending one.
  - channel must be whatsapp, sms, or email.
  - A successful tool result means ONLY that a payment-link record was created.
    It does NOT mean that the customer paid.
  - Never say "payment ho gaya" or "payment successful" because this tool
    succeeded. Wait for verified payment status from the backend/webhook.

CONVERSATION SAFETY

- The LLM is responsible for conversation understanding and structured
  extraction, not financial truth.
- Never change the outstanding amount through a tool call.
- Never mark a failed/pending payment as successful.
- Never create a promise that the customer did not clearly make.
- Before ending a call, if meaningful recovery information was obtained,
  update_recovery_case should be called first.
- If there was no clear promise, that is a valid outcome. The database should
  retain promise_to_pay="unclear" or "no" rather than forcing a promise.

When calling a tool, output ONLY this exact JSON format:
{"tool": "<tool_name>", "arguments": {<arguments>}}
""".strip()


def _get_recovery_context(args: dict) -> dict:
    session_id = args.get("session_id") or get_tool_session_id()
    case_id = args.get("case_id")
    return get_recovery_context(session_id=session_id, case_id=case_id)


def _update_recovery_case(args: dict) -> dict:
    session_id = args.get("session_id") or get_tool_session_id()
    case_id = args.get("case_id")

    return update_recovery_case(
        session_id=session_id,
        case_id=case_id,
        customer_intent=args.get("customer_intent"),
        promise_to_pay=args.get("promise_to_pay"),
        promise_date=args.get("promise_date"),
        recovery_reason=args.get("recovery_reason"),
        preferred_channel=args.get("preferred_channel"),
        sentiment=args.get("sentiment"),
        next_action=args.get("next_action"),
        notes=args.get("notes"),
    )


def _create_payment_link(args: dict) -> dict:
    session_id = args.get("session_id") or get_tool_session_id()
    case_id = args.get("case_id")
    return create_payment_link(
        session_id=session_id,
        case_id=case_id,
        channel=args.get("channel", "whatsapp"),
    )


_register_lock = threading.Lock()
_registered = False


def _register_recovery_tools():
    global _registered
    if _registered:
        return

    with _register_lock:
        if _registered:
            return

        register_tool(
            ToolSpec(
                name="get_recovery_context",
                description=(
                    "Read the active recovery case for the current call. "
                    "Returns customer, outstanding amount, payment status, "
                    "case type, previous recovery attempts, promise state, "
                    "and recent recovery events. Read-only."
                ),
                parameters={
                    "case_id": {
                        "type": "string",
                        "description": (
                            "Optional recovery case ID. Usually omit this and "
                            "let the server use the active call session."
                        ),
                    },
                },
                impl=_get_recovery_context,
                prompt_block=RECOVERY_PROMPT_BLOCK,
                terminal=False,
            ),
            override=True,
        )

        register_tool(
            ToolSpec(
                name="update_recovery_case",
                description=(
                    "Write structured information learned during the recovery "
                    "conversation, such as customer intent, whether a payment "
                    "promise was made, promise date when clearly stated, reason, "
                    "preferred channel, sentiment, and next recovery action. "
                    "Promise fields are optional and uncertain promises must "
                    "never be guessed."
                ),
                parameters={
                    "case_id": {
                        "type": "string",
                        "description": "Optional recovery case ID; normally omit.",
                    },
                    "customer_intent": {
                        "type": "string",
                        "description": (
                            "Optional. One of: willing_to_pay, not_willing_to_pay, "
                            "needs_more_time, payment_problem, already_paid, "
                            "disputed, unclear, unknown."
                        ),
                    },
                    "promise_to_pay": {
                        "type": "string",
                        "description": (
                            "Optional. Exactly yes, no, or unclear. Use yes only "
                            "for a clear customer commitment."
                        ),
                    },
                    "promise_date": {
                        "type": "string",
                        "description": (
                            "Optional. YYYY-MM-DD. Supply ONLY when the customer "
                            "clearly promised to pay and promise_to_pay is yes."
                        ),
                    },
                    "recovery_reason": {
                        "type": "string",
                        "description": "Optional concise reason given by the customer.",
                    },
                    "preferred_channel": {
                        "type": "string",
                        "description": "Optional preferred follow-up channel.",
                    },
                    "sentiment": {
                        "type": "string",
                        "description": "Optional conversation sentiment.",
                    },
                    "next_action": {
                        "type": "string",
                        "description": (
                            "Optional. One of: none, retry_payment, "
                            "send_payment_link, voice_call, whatsapp, follow_up, "
                            "human_escalation, wait_for_payment, close_case."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional concise structured note from the call.",
                    },
                },
                impl=_update_recovery_case,
                prompt_block=None,
                terminal=False,
            ),
            override=True,
        )

        register_tool(
            ToolSpec(
                name="create_payment_link",
                description=(
                    "Create a payment-link record for the active recovery case "
                    "when a payment link is appropriate. Success means only "
                    "that a link was created; it never means payment succeeded."
                ),
                parameters={
                    "case_id": {
                        "type": "string",
                        "description": "Optional recovery case ID; normally omit.",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Delivery channel: whatsapp, sms, or email.",
                    },
                },
                impl=_create_payment_link,
                prompt_block=None,
                terminal=False,
            ),
            override=True,
        )

        _registered = True
        logger.info(
            "recovery_tools_defs: registered get_recovery_context, "
            "update_recovery_case, create_payment_link"
        )


_register_recovery_tools()
