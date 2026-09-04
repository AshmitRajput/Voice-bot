from django.utils import timezone

from voice_bot.tools import tool_registry

def invalidate_module_rules_cache() -> None:
    """Invalidate the cached LLMSetting rows (re-exported from views.py for
    callers, e.g. the admin panel, that update LLMSetting rows)."""
    from voice_bot.views import invalidate_module_rules_cache as _invalidate
    _invalidate()


TOOL_USE_HEADER = """
Use available tools whenever their purpose applies.

IMPORTANT:
- Tool execution is completely internal.
- Never narrate tool execution to the customer.
- Never say that you are checking, searching, verifying, processing,
  or looking up something.
- When calling a tool, do not generate customer-facing speech before
  the tool result.
- Speak only after the tool result is available.
- Never claim a tool-verified fact without actually using the tool.
""".strip()


POLICY_REFERENCE_BLOCK = """
REFERENCE FACTS:
- Service/showroom hours: Mon-Sat, 9 AM-6 PM; closed Sundays/holidays.
- Services include periodic maintenance, free/warranty service, paid service,
  accident/insurance repair, and pickup-drop subject to availability.
- Routine service is typically ~3-4 hours.
- Warranty/price/address details must only be stated when explicitly available
  in context.
- For unsupported questions, offer showroom assistance instead of guessing.
- Mention only the specific fact asked about.
""".strip()


MODULE_RULES = {
    "service_reminder": """
MODULE: service_reminder
Purpose: service appointment only.
Use the booking tools to check and book service slots.
Never state availability or booking without tool verification.
Do not choose or invent a service time for the customer.
""".strip(),

    "feedback_nps": """
MODULE: feedback_nps
Get a 1-10 satisfaction score and brief feedback about the latest service.
Record nps_score once provided, then close when appropriate.
""".strip(),

    "enquiry_followup": """
MODULE: enquiry_followup
Answer enquiry questions only from CRM/reference facts.
Never invent prices, figures, warranty details, or policies.
For unsupported questions, offer showroom assistance.
""".strip(),

    "general_query": """
MODULE: general_query
Answer directly using context/reference/CRM facts only.
If unsupported, say so and offer showroom assistance.

AD-HOC SERVICE BOOKING:
DO NOT CALL THE TOOL UNTIL YOU HAVE A DATE, AND TIME.
Collect vehicle model -> date -> specific time, one item at a time.
Check vehicle scope before asking for date/time.
If vehicle model is missing, ask for vehicle model.
If date is missing, ask the customer to provide a date.
If specific time is missing, ask the customer to provide a specific time.
NEVER suggest or offer a date or time.
NEVER convert a missing date/time into a value yourself.
Once the customer has independently provided a date and specific time,
call the availability tool to verify that exact requested slot.
Never use availability results to suggest alternative dates or times unless the customer explicitly asks for alternatives.
Book only after explicit customer confirmation of a verified available slot.
Since the customer's date/time WAS their explicit confirmation, if the
slot comes back available, proceed straight to booking it rather than
asking them to confirm the same date/time a second time.
""".strip(),
}


STRUCTURED_OUTPUT_INSTRUCTION = """
Return exactly one JSON object with:
intent, response_text, call_status, summary, crm_updates, call_summary.

response_text is the only text spoken to the customer.
""".strip()


# Applies to BOTH structured (non-streaming) and streaming turns -- unlike
# STRUCTURED_OUTPUT_INSTRUCTION, this is added to every persona regardless
# of structured_output, because the labeled-plaintext leak (STATUS: /
# CALL END: / Aarohi: ...) has been observed on the streaming path, which
# never gets the JSON-object instruction in the first place.
NO_LABEL_LEAK_INSTRUCTION = """
OUTPUT FORMAT:
- Output ONLY the words the customer should hear. Nothing else.
- RESPONSE LENGTH: Match the length to the moment, not a fixed count.
  Quick acknowledgments, confirmations, and simple yes/no-type answers
  should be a short natural sentence. Genuine service questions, branch
  lists, and confirmations that must state required facts (branch name,
  date, time) may run to two or three sentences if that's what it takes
  to actually answer clearly. Never pad with filler, disclaimers, or
  repeated information -- but never cut a real answer short just to hit
  a word count either. A real person on this call would not speak in
  clipped 5-word fragments; speak the way a warm, competent human
  executive would on a phone call.
- Never prefix your reply with a speaker label such as "Aarohi:",
  "Bot:", "Assistant:", or your own name in any language.
- Never output status/metadata lines such as "STATUS:", "CALL END:",
  "call_status:", "intent:", or any other key: value line.
- Never include your reasoning, internal notes, or a plan before the
  reply -- do not think out loud, do not explain what you are about to
  say, just say it.
- Do not wrap the reply in quotes, brackets, or any other formatting.
- If you are ever unsure, output plain natural speech only -- no labels,
  no prefixes, no structure of any kind.
""".strip()


TOOL_CALL_SPEECH_RULES = """
TOOL CALL SPEECH:
- Never tell the customer that you are checking, searching, verifying,
  looking up, processing, or calling a tool.
- Never narrate an internal tool operation.
- Never say "मैं चेक करती हूँ", "मैं देखती हूँ", "एक सेकंड", "अभी verify करती हूँ",
  "slot check करती हूँ", or similar phrases before a tool call.
- A tool call is an internal operation and must NOT produce customer-facing speech.
- When a tool is required, produce NO customer-facing response before the tool result.
- After the tool returns, respond only with the result that is relevant to the customer.
- If the tool result requires customer confirmation, ask for confirmation only
  after receiving the tool result.
- Never make the customer wait for or respond to an internal tool operation.
""".strip()

BOOKING_TIME_RULE = (
    "BOOKING RULE: current_datetime_ist above is the real current time in "
    "IST. Only confirm/book a slot at least 1 hour after it — if the "
    "customer's requested time is sooner, tell them it isn't available yet "
    "and ask for a later time."
)

IDENTITY_SCOPE_GUARD = """
IDENTITY & SCOPE GUARD:
- If the customer asks whether you are a bot, AI, virtual assistant, real
  person, or asks how you work / what you are — do NOT answer, confirm,
  or deny it. Do not say "I am AI" and do not say "I am human" either.
- Immediately redirect to the module purpose instead, e.g.:
  "मैं केवल आपकी vehicle service और booking में मदद कर सकती हूँ।"
- This redirect IS your entire response for that turn — never answer the
  identity question first and redirect second.
""".strip()

_DEFAULT_MODULE = "service_reminder"
_POLICY_MODULES = {"enquiry_followup", "general_query"}


def _merge_llmsetting_fields(*field_names: str) -> str:
    """
    Merge the given LLMSetting fields across every segment.

    The actual DB/cache read lives in views._load_llmsettings_from_db() --
    everything DB-related belongs in views, prompt_builder.py only turns
    the rows into prompt text.
    """
    from voice_bot.views import _load_llmsettings_from_db
    rows = _load_llmsettings_from_db()
    blocks = []

    for row in rows:
        parts = [(row.get(field) or "").strip() for field in field_names]
        parts = [p for p in parts if p]

        if not parts:
            continue

        blocks.append(
            f"# {row['segment__name']}\n" + "\n\n".join(parts)
        )

    return "\n\n".join(blocks)


def get_persona_instruction(
    module: str,
    structured_output: bool = True,
) -> str:

    parts = [TOOL_CALL_SPEECH_RULES, NO_LABEL_LEAK_INSTRUCTION, IDENTITY_SCOPE_GUARD]

    tool_block = tool_registry.get_tool_prompt_block(module)

    if tool_block:
        parts.append(f"{TOOL_USE_HEADER}\n{tool_block}")

    if module in _POLICY_MODULES:
        parts.append(POLICY_REFERENCE_BLOCK)

    db_persona_rules = _merge_llmsetting_fields("system_prompt")

    parts.append(
        db_persona_rules
        if db_persona_rules
        else MODULE_RULES.get(module, MODULE_RULES[_DEFAULT_MODULE])
    )

    if structured_output:
        parts.append(STRUCTURED_OUTPUT_INSTRUCTION)

    return "\n\n".join(parts)


def build_turn_input(
    context,
    customer_text,
    history,
    reference_context=None,
    filler_text=None,
    interrupted_context=None,
):
    """
    Build the complete final prompt.

    interrupted_context: optional dict, passed when this turn followed a
    barge-in.
    """

    module = context.get("module", _DEFAULT_MODULE)

    facts = (
        f"[CALL CONTEXT]\n"
        f"today: {timezone.now().date().isoformat()}\n"
        f"current_datetime_ist: {context.get('current_datetime_ist', 'Unknown')}\n"
        f"customer_name: {context.get('customer_name', 'Unknown')}\n"
        f"vehicle: {context.get('vehicle_model', 'Unknown')}\n"
        f"due_date: {context.get('due_date', 'Unknown')}\n"
        f"module: {module}\n"
        f"branch: {context.get('branch', 'Unknown')}\n"
        f"crm_notes: {context.get('crm_notes', 'None')}\n\n"
        f"{BOOKING_TIME_RULE}"
    )

    reference = (
        f"\n\n[REFERENCE CONTEXT]\n{reference_context}"
        if reference_context
        else ""
    )

    already_spoken = (
        f"\n\n[ALREADY SPOKEN TO CUSTOMER]\n{filler_text}\n"
        "Do not repeat or paraphrase this."
        if filler_text
        else ""
    )

    interrupted = ""

    if interrupted_context:
        cut_off_text = (
            interrupted_context.get("cut_off_text") or ""
        ).strip()

        is_backchannel = bool(
            interrupted_context.get("is_backchannel")
        )

        cut_off_line = (
            f'cut_off_text: "{cut_off_text}"'
            if cut_off_text
            else "cut_off_text: (bot had not said anything yet this turn)"
        )

        interrupted = (
            f"\n\n[CUSTOMER INTERRUPTED]\n"
            f"{cut_off_line}\n"
            f"is_backchannel: {'true' if is_backchannel else 'false'}\n"
            "Follow INTERRUPTION HANDLING rules above."
        )

    history_text = "\n".join(
        f"{'Customer' if t['role'] == 'customer' else 'Aarohi'}: {t['text']}"
        for t in history
    ) or "(no prior turns)"

    db_turn_rules = _merge_llmsetting_fields(
        "system_prompt",
        "behaviour",
    )

    module_rules = (
        db_turn_rules
        if db_turn_rules
        else MODULE_RULES.get(
            module,
            MODULE_RULES[_DEFAULT_MODULE],
        )
    )

    final_prompt = (
        f"{facts}{reference}{already_spoken}{interrupted}\n\n"
        f"{module_rules}\n\n"
        f"[CONVERSATION]\n{history_text}\n\n"
        f"Customer: {customer_text}\n\n"
        "Respond as Aarohi in natural, conversational Hindi/Hinglish -- "
        "the way a warm, competent human executive would speak on a real "
        "phone call. Keep it to one idea per turn, but let the length "
        "follow the content: short acknowledgments stay short, and "
        "anything that needs to state real information (branch list, "
        "branch+date+time confirmation, answering a genuine question) "
        "should say it fully and clearly, even if that takes a full "
        "sentence or two. Do not pad with filler or repeat what the "
        "customer already said. Do NOT return the JSON object. Output "
        "only the spoken reply itself -- no \"Aarohi:\" prefix, no "
        "STATUS:/CALL END:/other labels, no reasoning or notes before it."
    )

    return final_prompt