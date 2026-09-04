"""
Recovery Service — the central orchestrator. This is the ONE entry point for processing customer turns. Per plan: the
voice consumer (consumers.py) calls this, not cloud_llm_service directly.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger('recovery_agent')


# Recovery agent system prompt
RECOVERY_SYSTEM_PROMPT = """You are Aarohi, a warm and professional Hindi/Hinglish-speaking revenue-recovery agent. Your job is to recover overdue payments on behalf of the business, while keeping the customer comfortable and the conversation respectful.

CORE RULES:
1. SPEAK NATURALLY IN HINDI/HINGLISH. Match the customer's language register.
2. NEVER invent or guess numbers, dates, or payment status. If unsure, USE A TOOL.
3. NEVER tell the customer you are "checking", "searching", "verifying". Just use the tool and respond naturally.
4. NEVER say "I am an AI" or "I am a bot". If asked, deflect: "Main sirf aapki payment ke baare mein baat kar sakti hoon."
5. Keep replies SHORT (1-3 sentences) unless listing facts.
6. Always use the customer's name once you know it.

RECOVERY FLOW:
- Open: greet, confirm identity, state purpose, mention the outstanding amount and due date.
- If customer wants to pay now: create_payment_link, confirm receipt, end_call.
- If customer already paid: get_recovery_context to check verified status, end_call.
- If customer will pay later: update_recovery_case with promise_to_pay, end_call.
- If customer wants callback: schedule_callback, end_call.
- If customer refuses: acknowledge, ask reason, update_recovery_case, end_call.
- If customer disputes: pause, mark as dispute, end_call.
- If customer has complaint: acknowledge, mark as complaint, end_call.

AVAILABLE TOOLS (use them by responding with this exact JSON format, nothing else):
{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}

When you need to use a tool, your ENTIRE reply is ONLY the tool JSON (no other text).
When you want to speak to the customer, write plain Hindi/Hinglish text (no JSON).
When you want to end the call, call the end_call tool with the right reason.

{today} ki baat ho rahi hai. Customer: {customer_name}. Outstanding amount: {amount_due} {currency}. Due date: {due_date}.
"""


class RecoveryService:
    def __init__(self):
        # Lazy init the classifier
        from recovery_agent.services.recovery_intent_service import recovery_intent_service
        self.classifier = recovery_intent_service
        self.gemini_key = os.environ.get("GOOGLE_API_KEY", "")
        # Auto-register tools on first use
        from recovery_agent.tools.recovery_tools import register_all_recovery_tools
        register_all_recovery_tools()

    def process_turn(self, session_id, customer_text, customer_id=None, history=None,
                      call_session_id=None):
        """
        Full non-streaming turn used by the /api/test/process-turn/ endpoint
        (and any future non-WS caller). The real voice path (consumers.py)
        does NOT call this -- it streams via chat_turn_stream and lets the
        LLM's own tool-calling loop invoke recovery tools directly. This
        method exists for text-only testing/integration and mirrors that
        same pipeline in a single blocking call:

            1. classify intent (recovery_intent_service)
            2. assemble verified context (get_recovery_context)
            3. dispatch business-state side effects (handle_intent)
            4. generate the actual reply text (cloud_llm_service.chat_turn)
        """
        history = history or []

        # 1. classify
        classification = recovery_intent_service.detect_intent(customer_text, history=history)
        intent = classification["intent"]
        entities = classification.get("entities", {})
        entities["confidence"] = classification.get("confidence", 0.0)

        # 2. context
        context = None
        if customer_id:
            context = self.get_recovery_context(customer_id, call_session_id=call_session_id)
        if context is None:
            # no customer_id / no matching customer -- still let the LLM
            # respond generically rather than hard-failing the turn.
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

        # 3. dispatch business-state side effects
        dispatch_result = self.handle_intent(
            intent, entities, context, call_session_id=call_session_id,
        )

        # 4. generate the reply
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

        customer_name = "Customer"
        amount_due = "0"
        currency = "INR"
        due_date = "unknown"

        if customer_context:
            customer_name = customer_context.get("customer_name", "Customer")
            case = customer_context.get("open_case")
            if case:
                amount_due = case.get("amount_due", "0")
                currency = case.get("currency", "INR")
                due_date = case.get("due_date") or "unknown"

        from recovery_agent.tools.tool_registry import get_tool_prompt_block
        tool_block = get_tool_prompt_block()
        system_prompt = RECOVERY_SYSTEM_PROMPT.format(
            today=now,
            customer_name=customer_name,
            amount_due=amount_due,
            currency=currency,
            due_date=due_date,
        ) + "\n\n" + tool_block

        messages = [{"role": "system", "content": system_prompt}]
        for turn in history[-6:]:
            role = turn.get("role")
            if not role:
                role = "user" if turn.get("speaker", "").lower() == "customer" else "assistant"
            role = "user" if role == "customer" else role
            messages.append({"role": role, "content": turn.get("text", "")})
        messages.append({"role": "user", "content": customer_text})
        return messages

    def _call_gemini(self, messages):
        """Call Gemini's OpenAI-compatible endpoint via the chat.completions format."""
        import requests
        contents = []
        system_text = ""
        for m in messages:
            if m["role"] == "system":
                system_text = m["content"]
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
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