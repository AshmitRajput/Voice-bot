"""
BharatRouter client backed by Gemma 4 31B via OpenAI-compatible API.

RecoverAI edition. Fixed vs previous version:
    - import was `voice_bot.tools.tool_registry` (Honda app, doesn't exist
      in this project) -> now `recovery_agent.tools.tool_registry`
    - generate_turn_stream executed at most ONE tool call and then stopped;
      non-terminal tools (get_recovery_context, update_recovery_case,
      create_payment_link) never got a follow-up LLM turn to act on their
      result. Ported the working multi-hop loop from the old KrutrimClient.
    - generate_call_summary / generate_intent_corrections were 100% Honda
      content (hardcoded "Aarohi", vehicle_model, invalid call_status
      values not in the real CallStatus enum). Rewritten against the real
      schemas.py and recovery_intent_service.py.
    - Persona is no longer hardcoded. get_persona_instruction() takes an
      optional persona_config (from LLMSetting in the admin) and falls
      back to a default persona: Riya, female, professional recovery
      agent, with escalation-pressure rules driven by call_attempt_number
      / promise_broken in context.

KrutrimClient dropped for now per decision to run BharatRouter only --
its hop-loop logic is what's now shared here, so it's trivial to bring
back later if needed.
"""

import json
import re
import time

import httpx
from openai import OpenAI
from django.conf import settings

from .schemas import TurnResult, LiveTurnResult
from .prompt_builder import get_persona_instruction, build_turn_input
from recovery_agent.tools import tool_registry
from recovery_agent.services.recovery_intent_service import INTENTS as _CANONICAL_INTENTS


FALLBACK_ERROR_TEXT = (
    "माफ़ कीजिये, थोड़ी technical दिक्कत आ रही है, "
    "हम आपको दोबारा call करेंगे।"
)
FALLBACK_TRUNCATED_TEXT = (
    "माफ़ कीजिये, थोड़ी technical दिक्कत आ रही है, "
    "थोड़ी देर रुकिए।"
)

_LABEL_LEAK_RE = re.compile(
    r"^\s*(STATUS|CALL END|Riya)\s*:",
    re.IGNORECASE,
)
_JSON_TOOL_RE = re.compile(r'\{"tool"\s*:\s*"')

_MAX_TOOL_HOPS = 2

LLM_PRICE_PER_MTOK_IN = 9
LLM_PRICE_PER_MTOK_OUT = 33

# Built from the real classifier vocabulary, not a hardcoded guess --
# previous version of this file hardcoded intent.py's old 9-class Honda
# vocabulary (booking, off_topic, upset...) which doesn't match what
# recovery_intent_service.py actually outputs.
INTENT_CLASSES_TEXT = ", ".join(_CANONICAL_INTENTS)


def calculate_llm_pricing(prompt_tokens: int, output_tokens: int) -> float:
    return (
        (prompt_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_IN
        + (output_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_OUT
    )


def _extract_labeled_reply(full_text: str) -> str:
    reply_lines = []
    capturing = False
    for line in full_text.split("\n"):
        match = re.match(r"^\s*Riya\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            capturing = True
            rest = match.group(1)
            if rest:
                reply_lines.append(rest)
            continue
        if capturing:
            if _LABEL_LEAK_RE.match(line):
                break
            reply_lines.append(line)
    return "\n".join(reply_lines).strip()


def _extract_json_from_text(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    brace_match = re.search(r'(\{.*\})', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ═══════════════════════════════════════════════════════════════
# DEFAULT PERSONA — fallback when no LLMSetting/persona_config exists.
# get_persona_instruction() in prompt_builder.py is expected to accept
# an optional `persona_config` dict pulled from the admin (name, gender,
# tone, opening_style) and merge it with this default; if persona_config
# is None or incomplete, THIS is what runs.
# ═══════════════════════════════════════════════════════════════

DEFAULT_PERSONA_NAME = "Riya"

DEFAULT_PERSONA_CORE = f"""You are {DEFAULT_PERSONA_NAME}, a professional female revenue-recovery
calling agent speaking Hindi/Hinglish, matched to the customer's register.

CORE RULES:
1. Never invent numbers, dates, or payment status -- use a tool if unsure.
2. Never say you are "checking" or "verifying" -- just use the tool and speak naturally.
3. Never say "I am an AI" or "I am a bot". If asked, deflect briefly and return to the topic.
4. Keep replies short (1-3 sentences) unless listing facts.
5. Use the customer's name once known.
6. Never threaten legal action, never misrepresent consequences, never raise your tone.
"""

# Escalation tiers keyed by call_attempt_number, applied on top of the
# core persona. promise_broken overrides tier selection upward when True.
DEFAULT_ESCALATION_RULES = """
PRESSURE / ESCALATION RULES (apply based on call_attempt_number and
promise_broken passed in context -- never invent these values yourself):

- Attempt 1, no broken promise:
  Polite and informative only. State the outstanding amount and due date
  once. Offer help (payment link, extension) without pushing.

- Attempt 2-3, no broken promise:
  Firmer, still respectful. Ask directly whether they intend to pay and
  by when. Ask for one specific, concrete commitment (a date). You may
  state real consequences factually (continued reminders, case stays
  open) -- never invent legal threats.

- Attempt 4+ OR promise_broken is true:
  Name the broken promise explicitly and factually (e.g. the date they
  committed to has passed). Do not accept a vague deflection
  ("dekhenge", "baad mein baat karenge") without pushing back ONCE for a
  concrete new date or a clear reason. If the customer gives a clear,
  final "no", stop pushing, acknowledge it, and record refused_to_pay --
  never harass or repeat the push after a clear refusal.

HARD FLOOR AT EVERY TIER:
  - Never threaten legal action, arrest, or anything not explicitly true.
  - Never raise your tone or use hostile language.
  - One push-back per deflection maximum, then respect what the customer says.
  - financial_hardship or dispute intents pause pressure entirely --
    switch to understanding/logging mode, do not keep pushing for payment.
"""


class BharatRouterClient:
    """OpenAI-compatible client for BharatRouter Gemma 4 31B."""

    def __init__(self):
        self._client = self._build_client()
        self.last_latency_seconds = None
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0

    def _build_client(self) -> OpenAI:
        api_key = settings.BHARATROUTER_API_KEY
        if not api_key:
            raise RuntimeError("BHARATROUTER_API_KEY not configured in settings")
        limits = httpx.Limits(
            max_keepalive_connections=10, max_connections=20, keepalive_expiry=120,
        )
        http_client = httpx.Client(limits=limits, http2=True, timeout=60)
        return OpenAI(
            api_key=api_key, base_url=settings.BHARATROUTER_BASE_URL,
            timeout=60, http_client=http_client,
        )

    def _reset_usage(self):
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0
        self.last_tool_call = None

    def _save_usage(self, usage):
        if usage is None:
            return
        self.last_prompt_token_count = getattr(usage, "prompt_tokens", 0) or 0
        self.last_output_token_count = getattr(usage, "completion_tokens", 0) or 0
        self.last_total_token_count = self.last_prompt_token_count + self.last_output_token_count
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details:
            self.last_cached_token_count = getattr(prompt_details, "cached_tokens", 0) or 0
        self.last_cache_hit = self.last_cached_token_count > 0

    def _build_messages(self, persona_text, turn_input, history=None, customer_history_summary=None):
        if customer_history_summary:
            summary_block = "\n".join(f"- {t}" for t in customer_history_summary)
            persona_text = (
                f"{persona_text}\n\n"
                f"[Everything the customer has said so far this call, in order -- "
                f"use this to avoid repeating questions already covered]\n{summary_block}"
            )
        messages = [{"role": "system", "content": persona_text}]
        if history:
            for turn in history[-6:]:
                role = "user" if turn["role"] == "customer" else "assistant"
                messages.append({"role": role, "content": turn["text"]})
        if isinstance(turn_input, str):
            messages.append({"role": "user", "content": turn_input})
        elif isinstance(turn_input, list):
            for content in turn_input:
                role = getattr(content, "role", "user")
                parts = getattr(content, "parts", [])
                text_parts = [p.text for p in parts if hasattr(p, "text")]
                if text_parts:
                    messages.append({"role": role, "content": "\n".join(text_parts)})
        return messages

    def _build_request_kwargs(self, messages, response_format="text", max_tokens=1024):
        kwargs = {
            "model": settings.BHARATROUTER_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 1,
            "top_p": 1,
            "extra_body": {
                "data_policy": getattr(settings, "BHARATROUTER_DATA_POLICY", "india_only"),
            },
        }
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    # ------------------------------------------------------------------
    # NON-STREAMING
    # ------------------------------------------------------------------

    def generate_turn(self, customer_text, context, history=None, reference_context=None,
                       cache_name=None, skip_cache_lookup=False, filler_text=None,
                       interrupted_context=None):
        history = history or []
        workflow = context.get("workflow", "revenue_recovery")
        persona_text = get_persona_instruction(
            workflow, persona_config=context.get("persona_config"),
        )
        turn_input = build_turn_input(
            context, customer_text, history,
            reference_context=reference_context, filler_text=filler_text,
            interrupted_context=interrupted_context,
        )
        messages = self._build_messages(persona_text, turn_input, history)
        start = time.monotonic()
        self._reset_usage()
        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(messages, response_format="json", max_tokens=1024)
            )
        except Exception as e:
            self.last_latency_seconds = time.monotonic() - start
            print(f"[BharatRouterClient] generate_content failed: {e}")
            return LiveTurnResult(
                intent="error", response_text=FALLBACK_ERROR_TEXT, call_status="error",
            ).model_dump(mode="json")

        self.last_latency_seconds = time.monotonic() - start
        self._save_usage(getattr(response, "usage", None))
        try:
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text)
            if parsed is None:
                raise ValueError("Could not extract JSON from response")
            return LiveTurnResult.model_validate(parsed).model_dump(mode="json")
        except Exception as e:
            finish_reason = None
            try:
                finish_reason = response.choices[0].finish_reason
            except Exception:
                pass
            print(f"[BharatRouterClient] malformed/truncated response "
                  f"(finish_reason={finish_reason}): {e}")
            return LiveTurnResult(
                intent="error", response_text=FALLBACK_TRUNCATED_TEXT, call_status="error",
            ).model_dump(mode="json")

    # ------------------------------------------------------------------
    # STREAMING
    # ------------------------------------------------------------------

    def _consume_stream(self, stream, hop: int):
        _leak_check_buffer = ""
        _leak_mode = None
        _leak_kind = None
        _leak_full_text = ""
        _last_usage = None
        self.last_tool_call = None
        _needs_followup = False
        _UNDECIDED_LENGTH_CAP = 40

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                _last_usage = usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            text = (delta.content or "") if delta else ""
            if not text:
                continue

            if _leak_mode is False:
                m = _JSON_TOOL_RE.search(text)
                if m:
                    pre = text[:m.start()]
                    if pre:
                        yield ("text", pre)
                    _leak_mode = True
                    _leak_kind = "json"
                    _leak_full_text = text[m.start():]
                    print(f"⚠️ [LLM] json leak detected mid-stream, mid-hop (hop {hop}) -- buffering")
                    continue
                yield ("text", text)
                continue

            _leak_check_buffer += text
            _leak_full_text += text

            if _leak_mode is None:
                stripped = _leak_check_buffer.lstrip()
                ready_to_decide = stripped and (
                    "\n" in _leak_check_buffer or len(stripped) >= _UNDECIDED_LENGTH_CAP
                )
                if ready_to_decide:
                    first_line = stripped.split("\n", 1)[0]
                    if stripped.startswith("{"):
                        _leak_mode = True
                        _leak_kind = "json"
                    elif _LABEL_LEAK_RE.match(first_line):
                        _leak_mode = True
                        _leak_kind = "labeled"
                    else:
                        _leak_mode = False
                    if _leak_mode:
                        print(f"⚠️ [LLM] {_leak_kind} leak detected mid-stream (hop {hop}) -- buffering")
                    else:
                        yield ("text", _leak_check_buffer)
                        _leak_check_buffer = ""

        if _leak_mode is None and _leak_check_buffer:
            yield ("text", _leak_check_buffer)
            _leak_check_buffer = ""

        if _leak_mode:
            recovered = ""
            if _leak_kind == "json":
                try:
                    parsed = json.loads(_leak_full_text)
                    tool_call = tool_registry.parse_tool_call(_leak_full_text)
                    if tool_call:
                        name, args = tool_call
                        exec_result = tool_registry.execute_tool(name, args)
                        self.last_tool_call = {"name": name, "arguments": args, "result": exec_result}
                        print(f"🔧 [LLM] tool executed: {name} -> {exec_result}")

                        if "result" in exec_result:
                            result_val = exec_result["result"]
                            tool_message = (
                                (result_val.get("message") or result_val.get("text")
                                 or json.dumps(result_val, ensure_ascii=False))
                                if isinstance(result_val, dict) else str(result_val)
                            )
                        else:
                            tool_message = f"माफ़ कीजिये, उसमें दिक्कत आई: {exec_result.get('error')}"

                        # Non-terminal tools hand result back to the LLM for a
                        # follow-up hop instead of speaking the raw tool
                        # message verbatim -- see generate_turn_stream loop.
                        spec = tool_registry.get_tool_specs()
                        spec_map = {s.name: s for s in spec}
                        matched = spec_map.get(name)
                        is_terminal = bool(matched and matched.terminal)

                        if is_terminal or hop >= _MAX_TOOL_HOPS:
                            recovered = tool_message
                        else:
                            _needs_followup = True
                    else:
                        recovered = parsed.get("response_text") or ""
                except Exception as e:
                    print(f"⚠️ [LLM] JSON leak parse failed ({e}); using fallback")
                    recovered = FALLBACK_TRUNCATED_TEXT
            else:
                recovered = _extract_labeled_reply(_leak_full_text)
                if not recovered:
                    print("⚠️ [LLM] labeled leak had no name: line to recover; using fallback")
                    recovered = FALLBACK_TRUNCATED_TEXT

            if recovered:
                yield ("text", recovered)

        yield ("done", {
            "function_call": self.last_tool_call,
            "needs_followup": _needs_followup,
            "model_content": None,
            "last_usage": _last_usage,
        })

    def generate_turn_stream(self, customer_text, context, history=None, reference_context=None,
                              cache_name=None, skip_cache_lookup=False, filler_text=None,
                              tools=None, interrupted_context=None):
        """Streaming path for voice bot. Non-terminal tool calls loop back
        into the LLM for up to _MAX_TOOL_HOPS extra completions within the
        same turn; terminal tools (end_call, schedule_callback) speak their
        own message and stop immediately."""
        history = history or []
        workflow = context.get("workflow", "revenue_recovery")
        customer_history_summary = context.get("customer_history_summary")
        _stream_start = time.monotonic()

        persona_text = get_persona_instruction(
            workflow, structured_output=False, persona_config=context.get("persona_config"),
        )
        if tools:
            tool_desc = self._format_tools_for_prompt(tools)
            persona_text = f"{persona_text}\n\n{tool_desc}"

        turn_input = build_turn_input(
            context, customer_text, history,
            reference_context=reference_context, filler_text=filler_text,
            interrupted_context=interrupted_context,
        )
        messages = self._build_messages(persona_text, turn_input, history, customer_history_summary)

        config = self._build_request_kwargs(messages, response_format="text", max_tokens=1024)
        config["stream"] = True
        config["stream_options"] = {"include_usage": True}

        self._reset_usage()

        current_messages = list(messages)
        hop = 0
        _summed_prompt_tokens = 0
        _summed_output_tokens = 0
        _summed_cached_tokens = 0
        _any_usage_seen = False

        while True:
            try:
                stream = self._client.chat.completions.create(**{**config, "messages": current_messages})
                result_state = None
                for kind, payload in self._consume_stream(stream, hop):
                    if kind == "text":
                        yield payload
                    else:
                        result_state = payload
            except Exception as e:
                self.last_latency_seconds = time.monotonic() - _stream_start
                print(f"[BharatRouterClient] stream failed (hop {hop}): {e}")
                if hop == 0:
                    yield FALLBACK_ERROR_TEXT
                return

            hop_usage = result_state.get("last_usage") if result_state else None
            if hop_usage is not None:
                _any_usage_seen = True
                _summed_prompt_tokens += getattr(hop_usage, "prompt_tokens", 0) or 0
                _summed_output_tokens += getattr(hop_usage, "completion_tokens", 0) or 0
                _ptd = getattr(hop_usage, "prompt_tokens_details", None)
                _summed_cached_tokens += getattr(_ptd, "cached_tokens", 0) or 0

            tool_call = result_state.get("function_call") if result_state else None
            needs_followup = bool(result_state and result_state.get("needs_followup"))

            if needs_followup and tool_call and hop < _MAX_TOOL_HOPS:
                tool_result_json = json.dumps(tool_call.get("result", {}), ensure_ascii=False)
                current_messages = current_messages + [
                    {"role": "assistant", "content": json.dumps(
                        {"tool": tool_call["name"], "arguments": tool_call["arguments"]},
                        ensure_ascii=False,
                    )},
                    {"role": "user", "content": (
                        f"[TOOL RESULT for {tool_call['name']}]\n{tool_result_json}\n\n"
                        "Choose EXACTLY ONE of the following for your entire reply -- never both:\n"
                        "1) Speak plain text to the customer (no JSON at all).\n"
                        "2) Call exactly one tool -- your ENTIRE reply must be ONLY that tool's JSON.\n\n"
                        "Do not call the same tool again with the same arguments, and do not "
                        "narrate that you used a tool."
                    )},
                ]
                hop += 1
                continue
            break

        if _any_usage_seen:
            self.last_prompt_token_count = _summed_prompt_tokens
            self.last_output_token_count = _summed_output_tokens
            self.last_total_token_count = _summed_prompt_tokens + _summed_output_tokens
            self.last_cached_token_count = _summed_cached_tokens

        self.last_latency_seconds = time.monotonic() - _stream_start
        print(f"[BHARAT-ROUTER] prompt_tokens={self.last_prompt_token_count} "
              f"output_tokens={self.last_output_token_count} "
              f"total_tokens={self.last_total_token_count} "
              f"latency={self.last_latency_seconds:.3f}s hops={hop + 1}")

    def _format_tools_for_prompt(self, tools: list) -> str:
        lines = [
            "You have access to the following tools. When you need to use a tool, "
            "respond with a JSON object in this exact format:\n",
            '{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}\n',
            "Available tools:\n",
        ]
        for tool in tools:
            if hasattr(tool, "name") and hasattr(tool, "description"):
                lines.append(f"- {tool.name}: {tool.description}")
                if tool.parameters:
                    lines.append("  Parameters:")
                    for prop_name, prop_info in tool.parameters.items():
                        desc = prop_info.get("description", "") if isinstance(prop_info, dict) else str(prop_info)
                        lines.append(f"    - {prop_name}: {desc}")
            elif isinstance(tool, dict):
                lines.append(f"- {tool.get('name', 'unknown')}: {tool.get('description', '')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # SCORING
    # ------------------------------------------------------------------

    def score_turn(self, customer_text, bot_response_text, filler_text,
                    turn_prompt_tokens, turn_output_tokens):
        prompt = (
            f"You are scoring ONE turn of a Hindi/Hinglish voice-bot phone call "
            f"for a revenue-recovery assistant named {DEFAULT_PERSONA_NAME}.\n\n"
            f"Customer said: {customer_text}\n\n"
        )
        if filler_text:
            prompt += (
                f"Filler line played while the real reply was generated: {filler_text}\n"
                "Score filler_accuracy (0-100): was this filler natural, brief, and "
                "appropriate -- not robotic, not misleading?\n\n"
            )
        prompt += (
            f"{DEFAULT_PERSONA_NAME}'s actual reply: {bot_response_text}\n"
            "Score accuracy (0-100): was this reply correct, on-topic, and appropriate?\n\n"
            'Return ONLY a JSON object: {"accuracy": <0-100 or null>, "filler_accuracy": '
            + ("<0-100 or null>" if filler_text else "null") + "}"
        )
        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs([{"role": "user", "content": prompt}],
                                              response_format="json", max_tokens=150)
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
        except Exception as e:
            print(f"[BharatRouterClient] score_turn failed: {e}")
            parsed = {}
        return {
            "accuracy": parsed.get("accuracy"),
            "filler_accuracy": parsed.get("filler_accuracy"),
            "llm_pricing": calculate_llm_pricing(turn_prompt_tokens, turn_output_tokens),
        }

    # ------------------------------------------------------------------
    # CALL SUMMARY -- rewritten against the real CallStatus enum and
    # RecoverySummaryData fields (no Honda booking/vehicle content).
    # ------------------------------------------------------------------

    def generate_call_summary(self, context, history, total_prompt_tokens=0, total_output_tokens=0):
        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else DEFAULT_PERSONA_NAME}: {t['text']}"
            for t in history
        ]
        history_block = "\n".join(history_lines) if history_lines else "(no turns recorded)"

        intent_history = context.get("intent_history") or []
        intent_block = (
            "\n".join(f"- turn intent: {i.get('intent')} (confidence: {i.get('confidence')})"
                      for i in intent_history)
            if intent_history else "(no per-turn intents recorded)"
        )

        prompt = (
            f"[CALL CONTEXT] customer_name: {context.get('customer_name', 'Unknown')} | "
            f"outstanding_amount: {context.get('outstanding_amount', 'Unknown')} | "
            f"due_date: {context.get('due_date', 'Unknown')} | "
            f"today: {context.get('today', 'Unknown')} | "
            f"call_attempt_number: {context.get('call_attempt_number', 1)} | "
            f"workflow: {context.get('workflow', 'revenue_recovery')}\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            f"Detected intents during the call (in order):\n{intent_block}\n\n"
            "The call has ended. Produce the final structured summary: "
            "intent (final intent of the call), response_text (leave as an empty "
            "string), call_status (MUST be exactly one of: in_progress, completed, "
            "declined, callback, wrong_number, do_not_call, closed, error -- use "
            "\"closed\" for a normal call that wrapped up without matching a more "
            "specific outcome), summary (an object with: recovery_outcome -- one of "
            "payment_verified, promise_recorded, payment_link_sent, refused, disputed, "
            "callback_scheduled, complaint_escalated, wrong_number, account_not_owned, "
            "none; promise_date if one was clearly given; next_action if stated or "
            "implied; refusal_count if the customer refused more than once this call), "
            "call_summary (a 4-5 sentence plain-English recap for staff use: who was "
            "called and why, what was discussed, the outcome, and any next action "
            "needed). filler_accuracy (0-100, how natural the filler lines were across "
            "the call; null if none played). llm_accuracy (0-100, how correct and "
            "on-topic the actual replies were across the call). "
            "Return ONLY the structured JSON output."
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs([{"role": "user", "content": prompt}],
                                              response_format="json", max_tokens=800)
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text)
            if parsed is None:
                raise ValueError("Could not extract JSON from summary response")

            filler_acc = parsed.get("filler_accuracy")
            llm_acc = parsed.get("llm_accuracy")
            scores = [s for s in (filler_acc, llm_acc) if isinstance(s, (int, float))]
            parsed["accuracy"] = round(sum(scores) / len(scores), 2) if scores else None

            summary_usage = getattr(response, "usage", None)
            summary_prompt_tok = getattr(summary_usage, "prompt_tokens", 0) or 0
            summary_output_tok = getattr(summary_usage, "completion_tokens", 0) or 0
            parsed["llm_pricing"] = calculate_llm_pricing(
                total_prompt_tokens + summary_prompt_tok,
                total_output_tokens + summary_output_tok,
            )
            return TurnResult.model_validate(parsed).model_dump(mode="json")

        except Exception as e:
            print(f"[BharatRouterClient] call summary generation failed: {e}")
            return TurnResult(
                intent="error", response_text="", call_status="closed",
                summary={"error": "summary_generation_failed"},
                call_summary="Call ended; automated summary generation failed. Manual review needed.",
                accuracy=None, filler_accuracy=None, llm_accuracy=None, llm_pricing=None,
            ).model_dump(mode="json")

    def generate_intent_corrections(self, history, intent_history, filler_history=None):
        if not intent_history:
            return []
        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else DEFAULT_PERSONA_NAME}: {t['text']}"
            for t in history
        ]
        history_block = "\n".join(history_lines) if history_lines else "(no turns recorded)"

        filler_history = filler_history or [None] * len(intent_history)
        turn_lines = [
            f"Turn {i}: detected_intent={ih.get('intent')} "
            f"(confidence={ih.get('confidence')}), filler_played={filler or '(none)'}"
            for i, (ih, filler) in enumerate(zip(intent_history, filler_history))
        ]
        turn_block = "\n".join(turn_lines)

        prompt = (
            f"You are auditing a Hindi/Hinglish voice-bot phone call for a "
            f"revenue-recovery assistant named {DEFAULT_PERSONA_NAME}, AFTER the call ended.\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            "For each customer turn, an intent classifier picked an intent and a filler "
            f"line was played based on it. What was detected/played, in order:\n{turn_block}\n\n"
            "With hindsight, review EVERY turn and say what the intent classifier SHOULD "
            f"have detected, and a more natural filler line. suggested_intent MUST be "
            f"exactly one of these classes -- the classifier's real vocabulary -- and "
            f"NOTHING else: {INTENT_CLASSES_TEXT}. If the original detection was already "
            "correct, repeat it back.\n\n"
            'Return ONLY: {"corrections": [{"turn_index": <int>, "suggested_intent": '
            '"<intent_code>", "suggested_filler": "<text>"}]}, one entry per turn, same order.'
        )
        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs([{"role": "user", "content": prompt}],
                                              response_format="json", max_tokens=800)
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
            return parsed.get("corrections", [])
        except Exception as e:
            print(f"[BharatRouterClient] generate_intent_corrections failed: {e}")
            return []