"""
Recovery Service — the central orchestrator. This is the ONE entry point for processing customer turns. Per plan: the
voice consumer (consumers.py) calls this, not cloud_llm_service directly. """

import json
import logging
import os
from typing import Optional

logger = logging.getLogger('recovery_agent')


# Recovery agent system prompt
RECOVERY_SYSTEM_PROMPT = """You are Aarohi, a warm and professional Hindi/Hinglish-speaking revenue-recovery agent. Your job is to recover overdue payments on behalf of the dealer, while keeping the customer comfortable and the conversation respectful.

CORE RULES:
1. SPEAK NATURALLY IN HINDI/HINGLISH. Match the customer's language register.
2. NEVER invent or guess numbers, dates, or payment status. If unsure, USE A TOOL.
3. NEVER tell the customer you are "checking", "searching", "verifying". Just use the tool and respond naturally.
4. NEVER say "I am an AI" or "I am a bot". If asked, deflect: "Main sirf aapki payment ke baare mein baat kar sakti hoon."
5. Keep replies SHORT (1-3 sentences) unless listing facts.
6. Always use the customer's name once you know it.

RECOVERY FLOW:
- Open: greet, confirm identity, state purpose, ask about outstanding balance.
- If customer wants to pay now: send_payment_link, confirm receipt, end_call.
- If customer already paid: verify_payment, end_call.
- If customer will pay later: record_payment_promise, end_call.
- If customer wants callback: schedule_callback, end_call.
- If customer refuses: acknowledge, ask reason, end_call.
- If customer disputes: pause, mark as dispute, end_call.
- If customer has complaint: acknowledge, mark as complaint, end_call.

AVAILABLE TOOLS (use them by responding with this exact JSON format, nothing else):
{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}

When you need to use a tool, your ENTIRE reply is ONLY the tool JSON (no other text).
When you want to speak to the customer, write plain Hindi/Hinglish text (no JSON).
When you want to end the call, call the end_call tool with the right reason.

{today} ki baat ho rahi hai. Customer: {customer_name}. Vehicle: {vehicle_model}.
"""


class RecoveryService:
    def __init__(self):
        # Lazy init the classifier
        from recovery_agent.services.intent_service import recovery_intent_service
        self.classifier = recovery_intent_service
        self.gemini_key = os.environ.get("GOOGLE_API_KEY", "")
        # Auto-register tools on first use
        from recovery_agent.tools.recovery_tools import register_all_recovery_tools
        register_all_recovery_tools()

    def process_turn(
        self,
        session_id: str,
        customer_text: str,
        customer_id: Optional[int] = None,
        dealer_id: Optional[int] = None,
        history: Optional[list] = None,
    ) -> dict:
        """
        Process a single customer turn end-to-end.
        Returns:
            {
                "intent":        <str>,
                "confidence":    <float>,
                "response_text": <str>,   # What the bot should SAY
                "tool_calls":    [list],  # Tools that were executed
                "state":         <dict>,  # Updated state snapshot
                "should_end_call": <bool>,
            }
        """
        history = history or []
        from recovery_agent.services.conversation_history import (
            get_state, set_recovery_intent, set_final_transcript, save_conversation,
            get_recovery_status, set_recovery_status,
        )
        from recovery_agent.tools import (
            tool_registry, set_tool_session, reset_tool_session,
            parse_tool_call, execute_tool, get_call_context,
        )

        # 1. Update state with new transcript
        save_conversation(session_id, "Customer", customer_text)
        set_final_transcript(session_id, customer_text)

        # 2. Load customer context
        customer_context = None
        if customer_id:
            from recovery_agent.services.crm_service import crm_service
            customer_context = crm_service.get_recovery_profile(customer_id)

        # 3. Build the LLM prompt as a mutable message list -- we keep
        # appending to this across tool hops so each re-call to the LLM
        # sees the full conversation, including tool results.
        messages = self._build_prompt(
            customer_text=customer_text,
            customer_context=customer_context,
            history=history,
        )

        # 4. Get the first LLM reply.
        response_text = self._get_llm_reply(messages, customer_text, customer_context)

        # 5. Tool-hop loop.
        #
        # Previously this loop only re-parsed response_text for another
        # tool-call JSON blob after each tool executed, and only updated
        # response_text when the tool's result happened to include a
        # "message" key. That worked by coincidence for create_payment_link,
        # schedule_callback, and end_call (which all return "message"), but
        # left the raw {"tool": ...} JSON as the customer-facing reply for
        # get_recovery_context and update_recovery_case, which don't.
        #
        # Fixed: after a non-terminal tool executes, append the tool call
        # and its result to the message history and call the LLM again for
        # a natural-language reply (standard function-calling pattern).
        # Terminal tools (end_call, schedule_callback -- anything with
        # ToolSpec.terminal=True) still end the turn immediately using
        # their own "message", since no further LLM turn should happen
        # after the call is ending.
        tool_calls_made = []
        should_end = False
        max_hops = 4
        hop = 0
        token = set_tool_session(session_id)
        try:
            while hop < max_hops:
                tool_call = parse_tool_call(response_text)
                if not tool_call:
                    break
                name, args = tool_call
                # Inject customer_id if missing
                if customer_id and "customer_id" not in args:
                    args["customer_id"] = customer_id
                logger.info(f"[RECOVERY] executing tool: {name}({args})")
                result = execute_tool(name, args)
                tool_calls_made.append({
                    "tool": name,
                    "arguments": args,
                    "result": result,
                })

                if result.get("terminal"):
                    should_end = True
                    response_text = result.get("result", {}).get(
                        "message", "Thank you. Goodbye."
                    )
                    break

                # Non-terminal tool: feed the tool result back to the LLM
                # as context and ask it to respond naturally (or call
                # another tool if it still needs to).
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(
                        {"tool": name, "arguments": args}, ensure_ascii=False
                    ),
                })
                tool_payload = result.get("result", result)
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Tool result for {name}]: "
                        f"{json.dumps(tool_payload, ensure_ascii=False)}\n\n"
                        "Use this to respond naturally to the customer in "
                        "Hindi/Hinglish, or call another tool if still "
                        "needed. Do not mention the tool, the database, or "
                        "that you 'checked' anything."
                    ),
                })
                response_text = self._get_llm_reply(
                    messages, customer_text, customer_context
                )
                hop += 1
        finally:
            reset_tool_session(token)

        # 6. Update intent state
        intent = "unclear"
        confidence = 0.0
        try:
            classification = self.classifier.detect_intent(customer_text, history=history)
            intent = classification["intent"]
            confidence = classification["confidence"]
            set_recovery_intent(session_id, intent, confidence, classification.get("entities", {}))
        except Exception as e:
            logger.warning(f"[RECOVERY] classification failed: {e}")

        # 7. Persist bot response
        save_conversation(session_id, "Aarohi", response_text)

        # 8. Return result
        return {
            "intent": intent,
            "confidence": confidence,
            "response_text": response_text,
            "tool_calls": tool_calls_made,
            "state": get_state(session_id),
            "should_end_call": should_end,
        }

    def _get_llm_reply(self, messages, customer_text, customer_context) -> str:
        """Call the LLM with the current message list, falling back to a
        simple keyword reply if the call fails or returns nothing."""
        reply = ""
        if self.gemini_key:
            try:
                reply = self._call_gemini(messages)
            except Exception as e:
                logger.exception(f"[RECOVERY] Gemini call failed: {e}")
                reply = ""
        if not reply:
            reply = self._fallback_reply(customer_text, customer_context)
        return reply

    def _build_prompt(self, customer_text, customer_context, history):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M %Z")
        customer_name = (customer_context or {}).get("customer_name", "Customer")
        vehicle_model = "Unknown"
        if customer_context and customer_context.get("vehicles"):
            vehicle_model = customer_context["vehicles"][0].get("model", "Unknown")
        # Get available tools as a prompt block
        from recovery_agent.tools import get_tool_prompt_block
        tool_block = get_tool_prompt_block()
        system_prompt = RECOVERY_SYSTEM_PROMPT.format(
            today=now,
            customer_name=customer_name,
            vehicle_model=vehicle_model,
        ) + "\n\n" + tool_block
        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        for turn in history[-6:]:
            role = "user" if turn.get("role") == "customer" else "assistant"
            if not role:
                role = "user" if turn.get("speaker", "").lower() == "customer" else "assistant"
            messages.append({"role": role, "content": turn.get("text", "")})
        messages.append({"role": "user", "content": customer_text})
        return messages

    def _call_gemini(self, messages):
        """Call Gemini's OpenAI-compatible endpoint via the chat.completions format."""
        import requests
        # Convert messages to Gemini format
        contents = []
        system_text = ""
        for m in messages:
            if m["role"] == "system":
                system_text = m["content"]
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
        # Prepend system text to first user message
        if system_text and contents:
            contents[0]["parts"][0]["text"] = system_text + "\n\n" + contents[0]["parts"][0]["text"]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={self.gemini_key}"
        )
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 300,
            },
        }
        resp = requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _fallback_reply(self, customer_text, customer_context):
        """Simple Hindi fallback reply when LLM is unavailable."""
        text = (customer_text or "").lower()
        if any(w in text for w in ["namaste", "hello", "hi"]):
            name = (customer_context or {}).get("customer_name", "ji")
            return f"Namaste {name} ji, main Aarohi bol rahi hoon."
        if any(w in text for w in ["payment", "paisa", "rupees", "kitna"]):
            return "Ji, main aapka balance check karke batata hoon."
        if any(w in text for w in ["bye", "alvida"]):
            return "Theek hai ji, dhanyawad. Namaste."
        return "Ji, bataiye."


recovery_service = RecoveryService()