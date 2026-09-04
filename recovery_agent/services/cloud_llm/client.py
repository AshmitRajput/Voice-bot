"""
BharatRouter client backed by Gemma 4 31B via OpenAI-compatible API.

Key differences from Gemini/Vertex:
- Uses standard OpenAI chat.completions (streaming + non-streaming)
- No context-cache mechanism (BharatRouter doesn't support it)
- No native tool/function calling — tools are embedded in system prompt
- Response is plain text; JSON extracted via parsing
- Connection pooling via httpx for performance
"""

import json
import re
import time

import httpx
from openai import OpenAI
from django.conf import settings

from .schemas import TurnResult, LiveTurnResult
from .prompt_builder import get_persona_instruction, build_turn_input
from voice_bot.tools import tool_registry


FALLBACK_ERROR_TEXT = (
    "माफ़ कीजिये, थोड़ी technical दिक्कत आ रही है, "
    "हम आपको दोबारा call करेंगे।"
)
FALLBACK_TRUNCATED_TEXT = (
    "माफ़ कीजिये, थोड़ी technical दिक्कत आ रही है, "
    "थोड़ी देर रुकिए।"
)

# Matches a labeled-plaintext leak line
_LABEL_LEAK_RE = re.compile(
    r"^\s*(STATUS|CALL END|Aarohi)\s*:",
    re.IGNORECASE,
)

_SENTENCE_END_CHARS = {".", "!", "?", "।", "॥"}

_MAX_TOOL_HOPS = 2

LLM_PRICE_PER_MTOK_IN = 9
LLM_PRICE_PER_MTOK_OUT = 33

INTENT_CLASSES_TEXT = (
    "booking, call_end, callback, complaint, generic, greeting, "
    "off_topic, query_general, upset"
)


def calculate_llm_pricing(prompt_tokens: int, output_tokens: int) -> float:
    """LLM cost in INR for given token counts, at ₹9/₹33 per 1M tok (in/out)."""
    return (
        (prompt_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_IN
        + (output_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_OUT
    )

def _extract_labeled_reply(full_text: str) -> str:
    """Recover customer-facing reply from labeled leak format."""
    reply_lines = []
    capturing = False

    for line in full_text.split("\n"):
        match = re.match(r"^\s*Aarohi\s*:\s*(.*)$", line, re.IGNORECASE)

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
    """Extract JSON object from text that may contain markdown or extra content."""
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting from markdown code blocks
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Try finding the first JSON object in the text
    brace_match = re.search(r'(\{.*\})', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


class BharatRouterClient:
    """
    OpenAI-compatible client for BharatRouter Gemma 4 31B.
    
    Connection pooling via httpx for performance.
    No context-cache (BharatRouter doesn't support it like Vertex).
    """

    def __init__(self):
        self._client = self._build_client()

        # Usage/latency tracking (same interface as old GeminiClient)
        self.last_latency_seconds = None
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0

    def _build_client(self) -> OpenAI:
        """Build OpenAI client with BharatRouter config and connection pooling."""
        api_key = settings.BHARATROUTER_API_KEY
        if not api_key:
            raise RuntimeError("BHARATROUTER_API_KEY not configured in settings")

        # Connection pooling for performance
        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=120,
        )

        http_client = httpx.Client(
            limits=limits,
            http2=True,
            timeout=60,
        )

        return OpenAI(
            api_key=api_key,
            base_url=settings.BHARATROUTER_BASE_URL,
            timeout=60,
            http_client=http_client,
        )

    def _reset_usage(self):
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0
        self.last_tool_call = None  

    def _save_usage(self, usage):
        """Save usage from OpenAI-compatible response."""
        if usage is None:
            return

        self.last_prompt_token_count = getattr(usage, "prompt_tokens", 0) or 0
        self.last_output_token_count = getattr(usage, "completion_tokens", 0) or 0
        self.last_total_token_count = self.last_prompt_token_count + self.last_output_token_count

        # Check for cached tokens in prompt_tokens_details
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details:
            self.last_cached_token_count = getattr(prompt_details, "cached_tokens", 0) or 0

        self.last_cache_hit = self.last_cached_token_count > 0

    def _build_messages(
        self,
        persona_text: str,
        turn_input: str,
        history: list | None = None,
        customer_history_summary: list | None = None,   # 🔥 NEW
    ) -> list[dict]:
        if customer_history_summary:
            summary_block = "\n".join(f"- {t}" for t in customer_history_summary)
            persona_text = (
                f"{persona_text}\n\n"
                f"[Everything the customer has said so far this call, in order — "
                f"use this to avoid repeating questions or re-asking things already "
                f"covered, even if it's not in the recent turns below]\n{summary_block}"
            )

        messages = [{"role": "system", "content": persona_text}]
        if history:
            for turn in history[-6:]:
                role = "user" if turn["role"] == "customer" else "assistant"
                messages.append({"role": role, "content": turn["text"]})

        # turn_input may be a string or list of Content objects
        if isinstance(turn_input, str):
            messages.append({"role": "user", "content": turn_input})
        elif isinstance(turn_input, list):
            # Convert Content objects to dict format
            for content in turn_input:
                role = getattr(content, "role", "user")
                parts = getattr(content, "parts", [])
                text_parts = []
                for part in parts:
                    if hasattr(part, "text"):
                        text_parts.append(part.text)
                if text_parts:
                    messages.append({
                        "role": role,
                        "content": "\n".join(text_parts)
                    })

        return messages

    def _build_request_kwargs(
        self,
        messages: list,
        response_format: str = "text",
        max_tokens: int = 1024,
    ) -> dict:
        """Build kwargs for chat.completions.create."""
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

    def generate_turn(
        self,
        customer_text: str,
        context: dict,
        history: list | None = None,
        reference_context: str | None = None,
        cache_name: str | None = None,  # Ignored — no cache support
        skip_cache_lookup: bool = False,  # Ignored
        filler_text: str | None = None,
        interrupted_context: dict | None = None,
    ) -> dict:
        """Non-streaming live turn."""
        history = history or []
        module = context.get("module", "service_reminder")
        persona_text = get_persona_instruction(module)

        turn_input = build_turn_input(
            context,
            customer_text,
            history,
            reference_context=reference_context,
            filler_text=filler_text,
            interrupted_context=interrupted_context,
        )

        messages = self._build_messages(persona_text, turn_input, history)

        start = time.monotonic()
        self._reset_usage()

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=messages,
                    response_format="json",
                    max_tokens=1024,
                )
            )
        except Exception as e:
            self.last_latency_seconds = time.monotonic() - start
            print(f"[BharatRouterClient] generate_content failed: {e}")
            return LiveTurnResult(
                intent="error",
                response_text=FALLBACK_ERROR_TEXT,
                call_status="error",
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

            print(
                f"[BharatRouterClient] malformed/truncated response "
                f"(finish_reason={finish_reason}, "
                f"len={len(response.choices[0].message.content or '')}): {e}"
            )

            return LiveTurnResult(
                intent="error",
                response_text=FALLBACK_TRUNCATED_TEXT,
                call_status="error",
            ).model_dump(mode="json")

    # ------------------------------------------------------------------
    # STREAMING
    # ------------------------------------------------------------------

    def _consume_stream(self, stream, hop: int):
        """
        Iterate a chat.completions stream, yielding text chunks.

        FIX: a chunk carrying finish_reason can ALSO carry the last content
            delta. Record it but keep going — do not `continue` here.
        NEW: Detects JSON tool calls, executes them via tool_registry, and
            surfaces metadata in the final done payload.
        """
        _leak_check_buffer = ""
        _leak_mode = None
        _leak_kind = None
        _leak_full_text = ""
        _last_usage = None
        self.last_tool_call = None          # Reset per stream

        _UNDECIDED_LENGTH_CAP = 40

        for chunk in stream:
            # Usage in final chunk
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                _last_usage = usage

            if not chunk.choices:
                continue

            choice = chunk.choices[0]

            # FIX: Don't skip this chunk — it may contain the final token.
            finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            text = (delta.content or "") if delta else ""
            if not text:
                continue

            if _leak_mode is False:
                yield ("text", text)
                continue

            _leak_check_buffer += text
            _leak_full_text += text

            if _leak_mode is None:
                stripped = _leak_check_buffer.lstrip()
                ready_to_decide = stripped and (
                    "\n" in _leak_check_buffer
                    or len(stripped) >= _UNDECIDED_LENGTH_CAP
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
                        print(
                            f"⚠️ [LLM] {_leak_kind} leak detected mid-stream "
                            f"(hop {hop}) -- buffering"
                        )
                    else:
                        yield ("text", _leak_check_buffer)
                        _leak_check_buffer = ""

        # Flush remaining undecided buffer
        if _leak_mode is None and _leak_check_buffer:
            yield ("text", _leak_check_buffer)
            _leak_check_buffer = ""

        if _leak_mode:
            recovered = ""

            if _leak_kind == "json":
                try:
                    parsed = json.loads(_leak_full_text)

                    # >>> Tool call detection & execution <<<
                    tool_call = tool_registry.parse_tool_call(_leak_full_text)
                    if tool_call:
                        name, args = tool_call
                        exec_result = tool_registry.execute_tool(name, args)
                        self.last_tool_call = {
                            "name": name,
                            "arguments": args,
                            "result": exec_result,
                        }
                        print(f"🔧 [LLM] tool executed: {name} -> {exec_result}")

                        if "result" in exec_result:
                            result_val = exec_result["result"]
                            if isinstance(result_val, dict):
                                recovered = (
                                    result_val.get("message")
                                    or result_val.get("text")
                                    or json.dumps(result_val, ensure_ascii=False)
                                )
                            else:
                                recovered = str(result_val)
                        else:
                            recovered = (
                                f"माफ़ कीजिये, उसमें दिक्कत आई: {exec_result.get('error')}"
                            )
                    else:
                        # Standard LiveTurnResult JSON
                        recovered = parsed.get("response_text") or ""
                except Exception as e:
                    print(
                        f"⚠️ [LLM] JSON leak parse failed ({e}); "
                        "using fallback"
                    )
                    recovered = FALLBACK_TRUNCATED_TEXT
            else:  # "labeled"
                recovered = _extract_labeled_reply(_leak_full_text)
                if not recovered:
                    print(
                        "⚠️ [LLM] labeled leak had no Aarohi: line to "
                        "recover; using fallback"
                    )
                    recovered = FALLBACK_TRUNCATED_TEXT

            if recovered:
                yield ("text", recovered)

        yield (
            "done",
            {
                "function_call": self.last_tool_call,
                "model_content": None,
                "last_usage": _last_usage,
            },
        )

    def generate_turn_stream(
        self,
        customer_text: str,
        context: dict,
        history: list | None = None,
        reference_context: str | None = None,
        cache_name: str | None = None,  # Ignored
        skip_cache_lookup: bool = False,  # Ignored
        filler_text: str | None = None,
        tools: list | None = None,  # Passed via prompt instead
        interrupted_context: dict | None = None,
    ):
        """
        Streaming path for voice bot.

        NOTE: BharatRouter doesn't support native tool/function calling.
        Tools must be described in the system prompt and responses parsed
        from the text output.
        """
        history = history or []
        module = context.get("module", "service_reminder")
        customer_history_summary = context.get("customer_history_summary")
        _stream_start = time.monotonic()

        persona_text = get_persona_instruction(
            module,
            structured_output=False,
        )

        # Append tool descriptions to persona if tools are requested
        if tools:
            tool_desc = self._format_tools_for_prompt(tools)
            persona_text = f"{persona_text}\n\n{tool_desc}"

        turn_input = build_turn_input(
            context,
            customer_text,
            history,
            reference_context=reference_context,
            filler_text=filler_text,
            interrupted_context=interrupted_context,
        )

        messages = self._build_messages(persona_text, turn_input, history, customer_history_summary)

        config = self._build_request_kwargs(
            messages=messages,
            response_format="text",
            max_tokens=1024,
        )
        config["stream"] = True
        config["stream_options"] = {"include_usage": True}

        self._reset_usage()

        try:
            stream = self._client.chat.completions.create(**config)

            result_state = None
            for kind, payload in self._consume_stream(stream, 0):
                if kind == "text":
                    yield payload
                else:
                    result_state = payload

        except Exception as e:
            self.last_latency_seconds = time.monotonic() - _stream_start
            print(f"[BharatRouterClient] stream failed: {e}")
            yield FALLBACK_ERROR_TEXT
            return

        if result_state is not None:
            _last_usage = result_state["last_usage"]

            if _last_usage is not None:
                self._save_usage(_last_usage)

        self.last_latency_seconds = time.monotonic() - _stream_start

        print(
            f"[BHARAT-ROUTER] "
            f"prompt_tokens={self.last_prompt_token_count} "
            f"output_tokens={self.last_output_token_count} "
            f"total_tokens={self.last_total_token_count}"
        )

        # No native function calling — tools handled via prompt parsing
        # If you need tool calls, parse the final text for tool invocation markers

    def _format_tools_for_prompt(self, tools: list) -> str:
        """Format tool declarations as text for inclusion in system prompt."""
        lines = [
            "You have access to the following tools. When you need to use a tool, "
            "respond with a JSON object in this exact format:\n",
            '{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}\n',
            "Available tools:\n",
        ]

        for tool in tools:
            if hasattr(tool, "function_declarations"):
                # GenAI / Vertex style (legacy compatibility)
                for fd in tool.function_declarations:
                    lines.append(f"- {fd.name}: {getattr(fd, 'description', 'No description')}")
                    params = getattr(fd, "parameters", None)
                    if params and hasattr(params, "properties"):
                        lines.append("  Parameters:")
                        for prop_name, prop_info in params.properties.items():
                            desc = getattr(prop_info, "description", "")
                            lines.append(f"    - {prop_name}: {desc}")
            elif hasattr(tool, "name") and hasattr(tool, "description"):
                # ToolSpec style (voice_bot tool_registry)  ← NEW branch
                lines.append(f"- {tool.name}: {tool.description}")
                if tool.parameters:
                    lines.append("  Parameters:")
                    for prop_name, prop_info in tool.parameters.items():
                        if isinstance(prop_info, dict):
                            desc = prop_info.get("description", "")
                        else:
                            desc = getattr(prop_info, "description", "")
                        lines.append(f"    - {prop_name}: {desc}")
            elif isinstance(tool, dict):
                lines.append(f"- {tool.get('name', 'unknown')}: {tool.get('description', '')}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # CALL SUMMARY
    # ------------------------------------------------------------------

    def score_turn(
        self,
        customer_text: str,
        bot_response_text: str,
        filler_text: str | None,
        turn_prompt_tokens: int,
        turn_output_tokens: int,
    ) -> dict:
        """
        One-shot LLM call scoring a single turn.

        accuracy        -> quality of Aarohi's actual reply this turn (this
                            IS the llm_accuracy score, stored directly on
                            ConversationTurn.accuracy -- no averaging at the
                            turn level, that's only done at the call level).
        filler_accuracy -> quality of the filler line played this turn
                            (None if no filler was played).
        llm_pricing     -> computed in Python from turn_prompt_tokens/
                            turn_output_tokens (the MAIN generation's usage
                            for this turn, not this scoring call's own usage).

        Deliberately does NOT call self._reset_usage()/_save_usage() -- that
        would clobber last_prompt_token_count/last_output_token_count on the
        shared client singleton, which the caller still needs for the NEXT
        turn's usage capture.
        """
        prompt = (
            "You are scoring ONE turn of a Hindi/Hinglish voice-bot phone "
            "call for a Honda service reminder assistant named Aarohi.\n\n"
            f"Customer said: {customer_text}\n\n"
        )
        if filler_text:
            prompt += (
                f"Filler line played while the real reply was generated: "
                f"{filler_text}\n"
                "Score filler_accuracy (0-100): was this filler natural, "
                "brief, and appropriate for what the customer just said -- "
                "not robotic, not misleading, and not contradicting the "
                "reply that followed?\n\n"
            )
        prompt += (
            f"Aarohi's actual reply: {bot_response_text}\n"
            "Score accuracy (0-100): was this reply correct, on-topic, and "
            "appropriate given what the customer said?\n\n"
            "Return ONLY a JSON object with exactly these keys: "
            '{"accuracy": <0-100 or null>, "filler_accuracy": '
            + ("<0-100 or null>" if filler_text else "null") + "}"
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=150,
                )
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
        except Exception as e:
            print(f"[{self.__class__.__name__}] score_turn failed: {e}")
            parsed = {}

        return {
            "accuracy": parsed.get("accuracy"),
            "filler_accuracy": parsed.get("filler_accuracy"),
            "llm_pricing": calculate_llm_pricing(turn_prompt_tokens, turn_output_tokens),
        }

    def generate_call_summary(
        self,
        context: dict,
        history: list,
        total_prompt_tokens: int = 0,
        total_output_tokens: int = 0,
    ) -> dict:
        """One-shot call summary after call ends.

        accuracy        -> computed in Python as avg(filler_accuracy, llm_accuracy),
                            never asked of the LLM directly.
        llm_pricing     -> whole-call token usage (every turn, summed by the caller
                            and passed in via total_prompt_tokens/total_output_tokens)
                            PLUS this summary-generation call's own usage.
        """
        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else 'Aarohi'}: {t['text']}"
            for t in history
        ]
        history_block = (
            "\n".join(history_lines)
            if history_lines
            else "(no turns recorded)"
        )

        intent_history = context.get("intent_history") or []
        intent_block = (
            "\n".join([
                f"- turn intent: {i.get('intent')} "
                f"(confidence: {i.get('confidence')})"
                for i in intent_history
            ])
            if intent_history
            else "(no per-turn intents recorded)"
        )

        prompt = (
            f"[CALL CONTEXT] customer_name: "
            f"{context.get('customer_name', 'Unknown')} | "
            f"vehicle: {context.get('vehicle_model', 'Unknown')} | "
            f"due_date: {context.get('due_date', 'Unknown')} | "
            f"today: {context.get('today', 'Unknown')} | "
            f"module: {context.get('module', 'service_reminder')}\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            f"Detected intents during the call (in order):\n"
            f"{intent_block}\n\n"
            "The call has ended. Based on the transcript above, produce the "
            "final structured summary: intent (final intent of the call), "
            "response_text (leave as an empty string -- not spoken to "
            "anyone), call_status (MUST be exactly one of: in_progress, "
            "booked, rescheduled, declined, callback, wrong_number, "
            "already_serviced, vehicle_sold, do_not_call, closed, error -- "
            "no other value is valid, even if it seems more descriptive; "
            "use \"closed\" for any normal call that wrapped up without "
            "matching one of the more specific outcomes), "
            "summary (booking_confirmed, appointment_datetime, next_action, "
            "nps_score, feedback_note, refusal_count -- fill in what "
            "applies, leave the rest null), crm_updates (only if the "
            "customer explicitly stated it in the transcript -- never "
            "guess or infer). crm_updates MUST be a JSON ARRAY of objects, "
            "never a plain object/dict -- each entry shaped exactly like "
            "{\"field\": \"vehicle_model\", \"new_value\": \"Honda City\"}, "
            "not {\"vehicle_model\": \"Honda City\"}. Valid field values: "
            "\"mobile_number\" for a corrected phone number, "
            "\"vehicle_model\" for the vehicle's model, \"vehicle_name\" "
            "for the vehicle's name/brand if stated separately from "
            "model, \"purchase_date\" if the customer stated when they "
            "bought the vehicle, \"last_service_date\" if they stated "
            "when it was last serviced, \"next_service_date\" and/or "
            "\"next_service_time\" if a new service/booking date or time "
            "was agreed on this call (separate from the existing "
            "appointment_datetime field in `summary`, which stays as the "
            "single combined value for the confirmed booking) -- emit one "
            "array entry per field actually stated; use an empty array "
            "[] if nothing qualifies, never a dict. call_summary (a 4-5 "
            "sentence plain-English recap for CRM/staff use covering who "
            "was called and why, what was discussed, the outcome, and any "
            "next action needed). "
            "filler_accuracy (a number from 0 to 100 scoring, ACROSS the "
            "whole call, how natural and appropriate Aarohi's filler lines "
            "were -- the short holding phrases spoken while the real reply "
            "was being generated -- judged only on whether they fit the "
            "moment and never contradicted the reply that followed; use "
            "null if no filler behaviour is visible in the transcript). "
            "llm_accuracy (a number from 0 to 100 scoring, ACROSS the whole "
            "call, how correct, on-topic, and appropriate Aarohi's actual "
            "replies were given what the customer said at each point -- "
            "this is about the substance of the responses, not the "
            "filler). "
            "Return ONLY the structured JSON output."
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=800,
                )
            )

            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text)
            if parsed is None:
                raise ValueError("Could not extract JSON from summary response")

            # accuracy is OURS to compute -- average of the two LLM-judged
            # scores, never something we ask the model to self-report.
            filler_acc = parsed.get("filler_accuracy")
            llm_acc = parsed.get("llm_accuracy")
            scores = [s for s in (filler_acc, llm_acc) if isinstance(s, (int, float))]
            parsed["accuracy"] = round(sum(scores) / len(scores), 2) if scores else None

            # llm_pricing: whole-call token usage (summed across turns by the
            # caller) PLUS this summary-generation call's own usage.
            summary_usage = getattr(response, "usage", None)
            summary_prompt_tok = getattr(summary_usage, "prompt_tokens", 0) or 0
            summary_output_tok = getattr(summary_usage, "completion_tokens", 0) or 0
            parsed["llm_pricing"] = calculate_llm_pricing(
                total_prompt_tokens + summary_prompt_tok,
                total_output_tokens + summary_output_tok,
            )

            return TurnResult.model_validate(parsed).model_dump(mode="json")

        except Exception as e:
            print(f"[{self.__class__.__name__}] call summary generation failed: {e}")
            return TurnResult(
                intent="error",
                response_text="",
                call_status="closed",
                summary={"error": "summary_generation_failed"},
                call_summary=(
                    "Call ended; automated summary generation failed. "
                    "Manual review needed."
                ),
                accuracy=None,
                filler_accuracy=None,
                llm_accuracy=None,
                llm_pricing=None,
            ).model_dump(mode="json")

    def generate_intent_corrections(
        self,
        history: list,
        intent_history: list,
        filler_history: list | None = None,
    ) -> list[dict]:
        """
        🔥 POST-CALL ONLY — called from finalize_call_summary() after the call
        has ended, never on the live turn path. One extra one-shot LLM call,
        reviewing the WHOLE transcript with hindsight, to say per turn what
        the fast filler-classifier's intent + filler line SHOULD have been.

        intent_history / filler_history: same length, same order — one entry
        per customer turn, built up live in consumers.py's
        self._intent_history / self._filler_history and handed straight
        through, so no extra DB read is needed to reconstruct them here.

        Returns [{"turn_index": <int>, "suggested_intent": "...",
        "suggested_filler": "..."}], turn_index 0-based, same order as the
        input lists — caller (views_admin.save_intent_corrections_sync)
        zips this back against the actual ConversationTurn rows.
        """
        if not intent_history:
            return []

        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else 'Aarohi'}: {t['text']}"
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
            "You are auditing a Hindi/Hinglish voice-bot phone call for a Honda "
            "service reminder assistant named Aarohi, AFTER the call has ended.\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            "For each customer turn, a FAST intent classifier picked an intent and "
            "a filler line was played based on it, BEFORE the real reply was "
            "generated. What was detected/played, in order:\n"
            f"{turn_block}\n\n"
            "With the benefit of hindsight, review EVERY turn and say what the "
            "intent classifier SHOULD have detected, and what filler line would "
            "have been more natural given what the customer actually said. "
            f"suggested_intent MUST be exactly one of these classes -- the "
            f"classifier's real output vocabulary -- and NOTHING else, never invent "
            f"a new label: {INTENT_CLASSES_TEXT}. If the original detection was "
            "already correct, repeat the same value back.\n\n"
            "Return ONLY a JSON object: "
            '{"corrections": [{"turn_index": <int>, "suggested_intent": '
            '"<intent_code>", "suggested_filler": "<text>"}]}, one entry per turn, '
            "same order as above."
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=800,
                )
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
            return parsed.get("corrections", [])
        except Exception as e:
            print(f"[{self.__class__.__name__}] generate_intent_corrections failed: {e}")
            return []
            
_JSON_TOOL_RE = re.compile(r'\{"tool"\s*:\s*"')

class KrutrimClient:
    """
    OpenAI-compatible client for Krutrim Cloud (Ola) Gemma 4 31B.

    Direct to the origin -- no router hop. Measured from Indore:
    TTFC p50 ~0.26s, p95 ~0.41s, p99 ~0.59s, 100% under 700ms over
    140 turns. Rs 9 in / Rs 33 out per 1M tokens, ~Rs 0.19 per
    15-turn call.

    Connection pooling via httpx for performance.
    No context-cache (Krutrim does not report cached_tokens today --
    the usage plumbing below is kept so it starts working the day
    they do).

    Rate limit: 200 requests / 60s. Watch this under concurrency.
    """

    def __init__(self):
        self._client = self._build_client()

        # Usage/latency tracking (same interface as BharatRouterClient)
        self.last_latency_seconds = None
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0

    def _build_client(self) -> OpenAI:
        """Build OpenAI client with Krutrim config and connection pooling."""
        api_key = settings.KRUTRIM_API_KEY
        if not api_key:
            raise RuntimeError("KRUTRIM_API_KEY not configured in settings")

        # Connection pooling for performance.
        # Keeping sockets warm matters here: a cold TLS handshake from
        # India costs more than the model's entire TTFC.
        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=120,
        )

        http_client = httpx.Client(
            limits=limits,
            http2=True,
            timeout=60,
        )

        return OpenAI(
            api_key=api_key,
            base_url=settings.KRUTRIM_BASE_URL,
            timeout=60,
            http_client=http_client,
        )

    def _reset_usage(self):
        self.last_cache_hit = False
        self.last_cached_token_count = 0
        self.last_total_token_count = 0
        self.last_prompt_token_count = 0
        self.last_output_token_count = 0
        self.last_tool_call = None  

    def _save_usage(self, usage):
        """Save usage from OpenAI-compatible response."""
        if usage is None:
            return

        self.last_prompt_token_count = getattr(usage, "prompt_tokens", 0) or 0
        self.last_output_token_count = getattr(usage, "completion_tokens", 0) or 0
        self.last_total_token_count = (
            self.last_prompt_token_count + self.last_output_token_count
        )

        # Krutrim does not populate this today, but the field is
        # OpenAI-standard so it will light up if they enable caching.
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details:
            self.last_cached_token_count = getattr(prompt_details, "cached_tokens", 0) or 0

        self.last_cache_hit = self.last_cached_token_count > 0

    def _build_messages(
        self,
        persona_text: str,
        turn_input: str,
        history: list | None = None,
        customer_history_summary: list | None = None,
    ) -> list[dict]:
        if customer_history_summary:
            summary_block = "\n".join(f"- {t}" for t in customer_history_summary)
            persona_text = (
                f"{persona_text}\n\n"
                f"[Everything the customer has said so far this call, in order — "
                f"use this to avoid repeating questions or re-asking things already "
                f"covered, even if it's not in the recent turns below]\n{summary_block}"
            )

        messages = [{"role": "system", "content": persona_text}]

        if history:
            for turn in history[-6:]:  # Limit to last 6 turns
                role = "user" if turn["role"] == "customer" else "assistant"
                messages.append({"role": role, "content": turn["text"]})

        # turn_input may be a string or list of Content objects
        if isinstance(turn_input, str):
            messages.append({"role": "user", "content": turn_input})
        elif isinstance(turn_input, list):
            # Convert Content objects to dict format
            for content in turn_input:
                role = getattr(content, "role", "user")
                parts = getattr(content, "parts", [])
                text_parts = []
                for part in parts:
                    if hasattr(part, "text"):
                        text_parts.append(part.text)
                if text_parts:
                    messages.append({
                        "role": role,
                        "content": "\n".join(text_parts)
                    })

        return messages

    def _build_request_kwargs(
        self,
        messages: list,
        response_format: str = "text",
        max_tokens: int = 1024,
    ) -> dict:
        """Build kwargs for chat.completions.create."""
        kwargs = {
            "model": settings.KRUTRIM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            # 0.4 measured noticeably steadier than 1.0 on this script.
            # Drop to 0.2 if replies still vary too much between calls.
            "temperature": 0.4,
            "top_p": 1,
        }

        # No extra_body: Krutrim is the origin, so there is no
        # data_policy / routing preference to pass along.

        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        return kwargs

    # ------------------------------------------------------------------
    # NON-STREAMING
    # ------------------------------------------------------------------

    def generate_turn(
        self,
        customer_text: str,
        context: dict,
        history: list | None = None,
        reference_context: str | None = None,
        cache_name: str | None = None,  # Ignored — no cache support
        skip_cache_lookup: bool = False,  # Ignored
        filler_text: str | None = None,
        interrupted_context: dict | None = None,
    ) -> dict:
        """Non-streaming live turn."""
        history = history or []
        module = context.get("module", "service_reminder")
        persona_text = get_persona_instruction(module)

        turn_input = build_turn_input(
            context,
            customer_text,
            history,
            reference_context=reference_context,
            filler_text=filler_text,
            interrupted_context=interrupted_context,
        )

        messages = self._build_messages(persona_text, turn_input, history)

        start = time.monotonic()
        self._reset_usage()

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=messages,
                    response_format="json",
                    max_tokens=1024,
                )
            )
        except Exception as e:
            self.last_latency_seconds = time.monotonic() - start
            print(f"[KrutrimClient] generate_content failed: {e}")
            return LiveTurnResult(
                intent="error",
                response_text=FALLBACK_ERROR_TEXT,
                call_status="error",
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

            print(
                f"[KrutrimClient] malformed/truncated response "
                f"(finish_reason={finish_reason}, "
                f"len={len(response.choices[0].message.content or '')}): {e}"
            )

            return LiveTurnResult(
                intent="error",
                response_text=FALLBACK_TRUNCATED_TEXT,
                call_status="error",
            ).model_dump(mode="json")

    # ------------------------------------------------------------------
    # STREAMING
    # ------------------------------------------------------------------

    def _consume_stream(self, stream, hop: int):
        """
        Iterate a chat.completions stream, yielding text chunks.

        FIX: a chunk carrying finish_reason can ALSO carry the last content
            delta. Record it but keep going — do not `continue` here.
        NEW: Detects JSON tool calls, executes them via tool_registry, and
            surfaces metadata in the final done payload.
        """
        _leak_check_buffer = ""
        _leak_mode = None
        _leak_kind = None
        _leak_full_text = ""
        _last_usage = None
        self.last_tool_call = None          # Reset per stream
        _needs_followup = False

        _UNDECIDED_LENGTH_CAP = 40

        for chunk in stream:
            # Usage in final chunk
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                _last_usage = usage

            if not chunk.choices:
                continue

            choice = chunk.choices[0]

            # FIX: Don't skip this chunk — it may contain the final token.
            finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            text = (delta.content or "") if delta else ""
            if not text:
                continue

            if _leak_mode is False:
                # NEW: even in pass-through mode, watch for a tool call
                # starting mid-text (e.g. after a sentence already streamed).
                combined = _leak_check_buffer + text  # _leak_check_buffer stays "" here normally
                m = _JSON_TOOL_RE.search(text)
                if m:
                    # yield only the text BEFORE the JSON starts
                    pre = text[:m.start()]
                    if pre:
                        yield ("text", pre)
                    # switch into json-leak buffering for the rest
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
                    "\n" in _leak_check_buffer
                    or len(stripped) >= _UNDECIDED_LENGTH_CAP
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
                        print(
                            f"⚠️ [LLM] {_leak_kind} leak detected mid-stream "
                            f"(hop {hop}) -- buffering"
                        )
                    else:
                        yield ("text", _leak_check_buffer)
                        _leak_check_buffer = ""

        # Flush remaining undecided buffer
        if _leak_mode is None and _leak_check_buffer:
            yield ("text", _leak_check_buffer)
            _leak_check_buffer = ""

        if _leak_mode:
            recovered = ""

            if _leak_kind == "json":
                try:
                    parsed = json.loads(_leak_full_text)

                    # >>> Tool call detection & execution <<<
                    tool_call = tool_registry.parse_tool_call(_leak_full_text)
                    if tool_call:
                        name, args = tool_call
                        exec_result = tool_registry.execute_tool(name, args)
                        self.last_tool_call = {
                            "name": name,
                            "arguments": args,
                            "result": exec_result,
                        }
                        print(f"🔧 [LLM] tool executed: {name} -> {exec_result}")

                        if "result" in exec_result:
                            result_val = exec_result["result"]
                            if isinstance(result_val, dict):
                                tool_message = (
                                    result_val.get("message")
                                    or result_val.get("text")
                                    or json.dumps(result_val, ensure_ascii=False)
                                )
                            else:
                                tool_message = str(result_val)
                        else:
                            tool_message = (
                                f"माफ़ कीजिये, उसमें दिक्कत आई: {exec_result.get('error')}"
                            )

                        # BUG FIX: this used to always speak the tool's raw
                        # `message` as the bot's final reply. That meant e.g.
                        # check_availability's canned "slot available" line
                        # became the spoken response with no LLM in the loop
                        # to actually act on it -- book_slot only ever got
                        # called on a LATER, separate customer turn, if at
                        # all. Non-terminal tools (default) now skip speaking
                        # here and instead get handed back to the LLM for a
                        # follow-up hop (see generate_turn_stream) so it can
                        # decide the next line/tool call itself. Terminal
                        # tools (e.g. end_call) still speak their own message
                        # directly -- there's nothing left to decide.
                        spec = tool_registry.get_tool_spec(name)
                        is_terminal = bool(spec and spec.terminal)

                        if is_terminal or hop >= _MAX_TOOL_HOPS:
                            recovered = tool_message
                        else:
                            _needs_followup = True
                    else:
                        # Standard LiveTurnResult JSON
                        recovered = parsed.get("response_text") or ""
                except Exception as e:
                    print(
                        f"⚠️ [LLM] JSON leak parse failed ({e}); "
                        "using fallback"
                    )
                    recovered = FALLBACK_TRUNCATED_TEXT
            else:  # "labeled"
                recovered = _extract_labeled_reply(_leak_full_text)
                if not recovered:
                    print(
                        "⚠️ [LLM] labeled leak had no Aarohi: line to "
                        "recover; using fallback"
                    )
                    recovered = FALLBACK_TRUNCATED_TEXT

            if recovered:
                yield ("text", recovered)

        yield (
            "done",
            {
                "function_call": self.last_tool_call,
                "needs_followup": _needs_followup,
                "model_content": None,
                "last_usage": _last_usage,
            },
        )

    def generate_turn_stream(
        self,
        customer_text: str,
        context: dict,
        history: list | None = None,
        reference_context: str | None = None,
        cache_name: str | None = None,  # Ignored
        skip_cache_lookup: bool = False,  # Ignored
        filler_text: str | None = None,
        tools: list | None = None,  # Passed via prompt instead
        interrupted_context: dict | None = None,
    ):
        """
        Streaming path for voice bot.

        NOTE: Gemma on Krutrim doesn't support native tool/function
        calling. Tools must be described in the system prompt and
        responses parsed from the text output.
        """
        history = history or []
        module = context.get("module", "service_reminder")
        customer_history_summary = context.get("customer_history_summary")
        _stream_start = time.monotonic()

        persona_text = get_persona_instruction(
            module,
            structured_output=False,
        )

        # Append tool descriptions to persona if tools are requested
        if tools:
            tool_desc = self._format_tools_for_prompt(tools)
            persona_text = f"{persona_text}\n\n{tool_desc}"

        turn_input = build_turn_input(
            context,
            customer_text,
            history,
            reference_context=reference_context,
            filler_text=filler_text,
            interrupted_context=interrupted_context,
        )

        messages = self._build_messages(persona_text, turn_input, history, customer_history_summary)

        config = self._build_request_kwargs(
            messages=messages,
            response_format="text",
            max_tokens=1024,
        )
        config["stream"] = True
        config["stream_options"] = {"include_usage": True}

        self._reset_usage()

        # BUG FIX: this used to make exactly one completion call and, if the
        # model called a tool, speak that tool's raw result as the final
        # reply -- there was no second round-trip for the LLM to actually
        # act on the tool result. That's why e.g. check_availability's
        # "slot available" message got spoken verbatim and book_slot only
        # ever fired on a LATER, separate customer turn (if the customer
        # happened to say something that read as booking intent again).
        # Now: non-terminal tools (check_availability, book_slot) hand
        # their result back to the model for up to _MAX_TOOL_HOPS extra
        # completions within this SAME turn, so it can decide what to say
        # and/or chain straight into the next tool call. Terminal tools
        # (end_call) never loop -- they speak their own message and we stop.
        current_messages = list(messages)
        hop = 0
        _summed_prompt_tokens = 0
        _summed_output_tokens = 0
        _summed_cached_tokens = 0
        _any_usage_seen = False

        while True:
            try:
                stream = self._client.chat.completions.create(
                    **{**config, "messages": current_messages}
                )

                result_state = None
                for kind, payload in self._consume_stream(stream, hop):
                    if kind == "text":
                        yield payload
                    else:
                        result_state = payload

            except Exception as e:
                self.last_latency_seconds = time.monotonic() - _stream_start
                print(f"[KrutrimClient] stream failed (hop {hop}): {e}")
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
                tool_result_json = json.dumps(
                    tool_call.get("result", {}), ensure_ascii=False
                )
                current_messages = current_messages + [
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {"tool": tool_call["name"], "arguments": tool_call["arguments"]},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"[TOOL RESULT for {tool_call['name']}]\n"
                            f"{tool_result_json}\n\n"
                            "Choose EXACTLY ONE of the following for your entire reply -- "
                            "never both:\n"
                            "1) Speak plain text to the customer (no JSON at all), if you "
                            "are not ending the call and not calling another tool.\n"
                            "2) Call exactly one tool, following the tool/module rules "
                            "above -- your ENTIRE reply must be ONLY that tool's JSON, "
                            "with nothing before or after it.\n\n"
                            "If you want to confirm something to the customer AND end the "
                            "call (e.g. right after a booking is confirmed), do NOT write "
                            "the confirmation as separate spoken text first -- put that "
                            "exact sentence inside the tool's own closing_message argument "
                            "and call the tool. The tool call IS how that line gets "
                            "spoken; writing it twice is a bug.\n"
                            "Do not call the same tool again with the same arguments, and "
                            "do not narrate that you used a tool."
                        ),
                    },
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

        print(
            f"[KRUTRIM] "
            f"prompt_tokens={self.last_prompt_token_count} "
            f"output_tokens={self.last_output_token_count} "
            f"total_tokens={self.last_total_token_count} "
            f"latency={self.last_latency_seconds:.3f}s "
            f"hops={hop + 1}"
        )

        # No native function calling — tools handled via prompt parsing.
        # Tool call chaining across hops is handled above in this method.

    def _format_tools_for_prompt(self, tools: list) -> str:
        """Format tool declarations as text for inclusion in system prompt."""
        lines = [
            "You have access to the following tools. When you need to use a tool, "
            "respond with a JSON object in this exact format:\n",
            '{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}\n',
            "Available tools:\n",
        ]

        for tool in tools:
            if hasattr(tool, "function_declarations"):
                # GenAI / Vertex style (legacy compatibility)
                for fd in tool.function_declarations:
                    lines.append(f"- {fd.name}: {getattr(fd, 'description', 'No description')}")
                    params = getattr(fd, "parameters", None)
                    if params and hasattr(params, "properties"):
                        lines.append("  Parameters:")
                        for prop_name, prop_info in params.properties.items():
                            desc = getattr(prop_info, "description", "")
                            lines.append(f"    - {prop_name}: {desc}")
            elif hasattr(tool, "name") and hasattr(tool, "description"):
                # ToolSpec style (voice_bot tool_registry)  ← NEW branch
                lines.append(f"- {tool.name}: {tool.description}")
                if tool.parameters:
                    lines.append("  Parameters:")
                    for prop_name, prop_info in tool.parameters.items():
                        if isinstance(prop_info, dict):
                            desc = prop_info.get("description", "")
                        else:
                            desc = getattr(prop_info, "description", "")
                        lines.append(f"    - {prop_name}: {desc}")
            elif isinstance(tool, dict):
                lines.append(f"- {tool.get('name', 'unknown')}: {tool.get('description', '')}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # CALL SUMMARY
    # ------------------------------------------------------------------
    def score_turn(
        self,
        customer_text: str,
        bot_response_text: str,
        filler_text: str | None,
        turn_prompt_tokens: int,
        turn_output_tokens: int,
    ) -> dict:
        """
        One-shot LLM call scoring a single turn.

        accuracy        -> quality of Aarohi's actual reply this turn (this
                            IS the llm_accuracy score, stored directly on
                            ConversationTurn.accuracy -- no averaging at the
                            turn level, that's only done at the call level).
        filler_accuracy -> quality of the filler line played this turn
                            (None if no filler was played).
        llm_pricing     -> computed in Python from turn_prompt_tokens/
                            turn_output_tokens (the MAIN generation's usage
                            for this turn, not this scoring call's own usage).

        Deliberately does NOT call self._reset_usage()/_save_usage() -- that
        would clobber last_prompt_token_count/last_output_token_count on the
        shared client singleton, which the caller still needs for the NEXT
        turn's usage capture.
        """
        prompt = (
            "You are scoring ONE turn of a Hindi/Hinglish voice-bot phone "
            "call for a Honda service reminder assistant named Aarohi.\n\n"
            f"Customer said: {customer_text}\n\n"
        )
        if filler_text:
            prompt += (
                f"Filler line played while the real reply was generated: "
                f"{filler_text}\n"
                "Score filler_accuracy (0-100): was this filler natural, "
                "brief, and appropriate for what the customer just said -- "
                "not robotic, not misleading, and not contradicting the "
                "reply that followed?\n\n"
            )
        prompt += (
            f"Aarohi's actual reply: {bot_response_text}\n"
            "Score accuracy (0-100): was this reply correct, on-topic, and "
            "appropriate given what the customer said?\n\n"
            "Return ONLY a JSON object with exactly these keys: "
            '{"accuracy": <0-100 or null>, "filler_accuracy": '
            + ("<0-100 or null>" if filler_text else "null") + "}"
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=150,
                )
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
        except Exception as e:
            print(f"[{self.__class__.__name__}] score_turn failed: {e}")
            parsed = {}

        return {
            "accuracy": parsed.get("accuracy"),
            "filler_accuracy": parsed.get("filler_accuracy"),
            "llm_pricing": calculate_llm_pricing(turn_prompt_tokens, turn_output_tokens),
        }

    def generate_call_summary(
        self,
        context: dict,
        history: list,
        total_prompt_tokens: int = 0,
        total_output_tokens: int = 0,
    ) -> dict:
        """One-shot call summary after call ends.

        accuracy        -> computed in Python as avg(filler_accuracy, llm_accuracy),
                            never asked of the LLM directly.
        llm_pricing     -> whole-call token usage (every turn, summed by the caller
                            and passed in via total_prompt_tokens/total_output_tokens)
                            PLUS this summary-generation call's own usage.
        """
        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else 'Aarohi'}: {t['text']}"
            for t in history
        ]
        history_block = (
            "\n".join(history_lines)
            if history_lines
            else "(no turns recorded)"
        )

        intent_history = context.get("intent_history") or []
        intent_block = (
            "\n".join([
                f"- turn intent: {i.get('intent')} "
                f"(confidence: {i.get('confidence')})"
                for i in intent_history
            ])
            if intent_history
            else "(no per-turn intents recorded)"
        )

        prompt = (
            f"[CALL CONTEXT] customer_name: "
            f"{context.get('customer_name', 'Unknown')} | "
            f"vehicle: {context.get('vehicle_model', 'Unknown')} | "
            f"due_date: {context.get('due_date', 'Unknown')} | "
            f"today: {context.get('today', 'Unknown')} | "
            f"module: {context.get('module', 'service_reminder')}\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            f"Detected intents during the call (in order):\n"
            f"{intent_block}\n\n"
            "The call has ended. Based on the transcript above, produce the "
            "final structured summary: intent (final intent of the call), "
            "response_text (leave as an empty string -- not spoken to "
            "anyone), call_status (MUST be exactly one of: in_progress, "
            "booked, rescheduled, declined, callback, wrong_number, "
            "already_serviced, vehicle_sold, do_not_call, closed, error -- "
            "no other value is valid, even if it seems more descriptive; "
            "use \"closed\" for any normal call that wrapped up without "
            "matching one of the more specific outcomes), "
            "summary (booking_confirmed, appointment_datetime, next_action, "
            "nps_score, feedback_note, refusal_count -- fill in what "
            "applies, leave the rest null), crm_updates (only if the "
            "customer explicitly stated it in the transcript -- never "
            "guess or infer). crm_updates MUST be a JSON ARRAY of objects, "
            "never a plain object/dict -- each entry shaped exactly like "
            "{\"field\": \"vehicle_model\", \"new_value\": \"Honda City\"}, "
            "not {\"vehicle_model\": \"Honda City\"}. Valid field values: "
            "\"mobile_number\" for a corrected phone number, "
            "\"vehicle_model\" for the vehicle's model, \"vehicle_name\" "
            "for the vehicle's name/brand if stated separately from "
            "model, \"purchase_date\" if the customer stated when they "
            "bought the vehicle, \"last_service_date\" if they stated "
            "when it was last serviced, \"next_service_date\" and/or "
            "\"next_service_time\" if a new service/booking date or time "
            "was agreed on this call (separate from the existing "
            "appointment_datetime field in `summary`, which stays as the "
            "single combined value for the confirmed booking) -- emit one "
            "array entry per field actually stated; use an empty array "
            "[] if nothing qualifies, never a dict. call_summary (a 4-5 "
            "sentence plain-English recap for CRM/staff use covering who "
            "was called and why, what was discussed, the outcome, and any "
            "next action needed). "
            "filler_accuracy (a number from 0 to 100 scoring, ACROSS the "
            "whole call, how natural and appropriate Aarohi's filler lines "
            "were -- the short holding phrases spoken while the real reply "
            "was being generated -- judged only on whether they fit the "
            "moment and never contradicted the reply that followed; use "
            "null if no filler behaviour is visible in the transcript). "
            "llm_accuracy (a number from 0 to 100 scoring, ACROSS the whole "
            "call, how correct, on-topic, and appropriate Aarohi's actual "
            "replies were given what the customer said at each point -- "
            "this is about the substance of the responses, not the "
            "filler). "
            "Return ONLY the structured JSON output."
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=800,
                )
            )

            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text)
            if parsed is None:
                raise ValueError("Could not extract JSON from summary response")

            # accuracy is OURS to compute -- average of the two LLM-judged
            # scores, never something we ask the model to self-report.
            filler_acc = parsed.get("filler_accuracy")
            llm_acc = parsed.get("llm_accuracy")
            scores = [s for s in (filler_acc, llm_acc) if isinstance(s, (int, float))]
            parsed["accuracy"] = round(sum(scores) / len(scores), 2) if scores else None

            # llm_pricing: whole-call token usage (summed across turns by the
            # caller) PLUS this summary-generation call's own usage.
            summary_usage = getattr(response, "usage", None)
            summary_prompt_tok = getattr(summary_usage, "prompt_tokens", 0) or 0
            summary_output_tok = getattr(summary_usage, "completion_tokens", 0) or 0
            parsed["llm_pricing"] = calculate_llm_pricing(
                total_prompt_tokens + summary_prompt_tok,
                total_output_tokens + summary_output_tok,
            )

            return TurnResult.model_validate(parsed).model_dump(mode="json")

        except Exception as e:
            print(f"[{self.__class__.__name__}] call summary generation failed: {e}")
            return TurnResult(
                intent="error",
                response_text="",
                call_status="closed",
                summary={"error": "summary_generation_failed"},
                call_summary=(
                    "Call ended; automated summary generation failed. "
                    "Manual review needed."
                ),
                accuracy=None,
                filler_accuracy=None,
                llm_accuracy=None,
                llm_pricing=None,
            ).model_dump(mode="json")

    def generate_intent_corrections(
        self,
        history: list,
        intent_history: list,
        filler_history: list | None = None,
    ) -> list[dict]:
        """
        🔥 POST-CALL ONLY — called from finalize_call_summary() after the call
        has ended, never on the live turn path. One extra one-shot LLM call,
        reviewing the WHOLE transcript with hindsight, to say per turn what
        the fast filler-classifier's intent + filler line SHOULD have been.

        intent_history / filler_history: same length, same order — one entry
        per customer turn, built up live in consumers.py's
        self._intent_history / self._filler_history and handed straight
        through, so no extra DB read is needed to reconstruct them here.

        Returns [{"turn_index": <int>, "suggested_intent": "...",
        "suggested_filler": "..."}], turn_index 0-based, same order as the
        input lists — caller (views_admin.save_intent_corrections_sync)
        zips this back against the actual ConversationTurn rows.
        """
        if not intent_history:
            return []

        history_lines = [
            f"{'Customer' if t['role'] == 'customer' else 'Aarohi'}: {t['text']}"
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
            "You are auditing a Hindi/Hinglish voice-bot phone call for a Honda "
            "service reminder assistant named Aarohi, AFTER the call has ended.\n\n"
            f"Full call transcript:\n{history_block}\n\n"
            "For each customer turn, a FAST intent classifier picked an intent and "
            "a filler line was played based on it, BEFORE the real reply was "
            "generated. What was detected/played, in order:\n"
            f"{turn_block}\n\n"
            "With the benefit of hindsight, review EVERY turn and say what the "
            "intent classifier SHOULD have detected, and what filler line would "
            "have been more natural given what the customer actually said. "
            f"suggested_intent MUST be exactly one of these classes -- the "
            f"classifier's real output vocabulary -- and NOTHING else, never invent "
            f"a new label: {INTENT_CLASSES_TEXT}. If the original detection was "
            "already correct, repeat the same value back.\n\n"
            "Return ONLY a JSON object: "
            '{"corrections": [{"turn_index": <int>, "suggested_intent": '
            '"<intent_code>", "suggested_filler": "<text>"}]}, one entry per turn, '
            "same order as above."
        )

        try:
            response = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    messages=[{"role": "user", "content": prompt}],
                    response_format="json",
                    max_tokens=800,
                )
            )
            raw_text = response.choices[0].message.content or ""
            parsed = _extract_json_from_text(raw_text) or {}
            return parsed.get("corrections", [])
        except Exception as e:
            print(f"[{self.__class__.__name__}] generate_intent_corrections failed: {e}")
            return []