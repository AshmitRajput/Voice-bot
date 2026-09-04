"""
prompt_builder.py — RecoverAI edition.

Fixed vs previous version:
    - voice_bot.tools / voice_bot.views imports -> recovery_agent
    - MODULE_RULES was Honda's 4-module system (service_reminder,
      feedback_nps, enquiry_followup, general_query) with booking-slot
      logic baked in. Replaced with a single recovery-conversation rule
      set plus dynamic escalation tiers.
    - Persona was hardcoded to "Aarohi". get_persona_instruction() now
      takes an optional persona_config dict (name/gender/tone/opening_style,
      expected to come from LLMSetting via the admin) and falls back to a
      default persona: Riya, professional female recovery agent, with
      pressure/escalation rules driven by call_attempt_number and
      promise_broken in context.
    - build_turn_input() facts block now surfaces amount_due/due_date/
      call_attempt_number/promise_broken/recovery_status instead of
      vehicle_model/branch (Honda fields that don't exist on RecoveryCase).
    - STRUCTURED_OUTPUT_INSTRUCTION matches the real TurnResult schema --
      dropped crm_updates, which has no equivalent field on
      RecoverySummaryData.
"""

from django.utils import timezone

from recovery_agent.tools import tool_registry


def invalidate_module_rules_cache() -> None:
    """Invalidate the cached LLMSetting rows (re-exported from views.py)."""
    from recovery_agent.views import invalidate_module_rules_cache as _invalidate
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


# ═══════════════════════════════════════════════════════════════
# DEFAULT PERSONA — Riya. Used whenever persona_config is None or
# incomplete. persona_config is expected to come from an LLMSetting row
# via the admin (fields like name/gender/tone/opening_style/system_prompt);
# any field the admin hasn't set falls back to the corresponding default
# below rather than leaving a gap in the prompt.
# ═══════════════════════════════════════════════════════════════

DEFAULT_PERSONA_NAME = "Riya"
DEFAULT_PERSONA_GENDER = "female"
DEFAULT_PERSONA_TONE = "warm but firm, professional"

DEFAULT_PERSONA_CORE_TEMPLATE = """You are {name}, a professional {gender} revenue-recovery calling
agent speaking Hindi/Hinglish, matched to the customer's register. Your
tone is {tone}.

CORE RULES:
1. Never invent numbers, dates, or payment status -- use a tool if unsure.
2. Never say you are "checking" or "verifying" -- just use the tool and speak naturally.
3. Never say "I am an AI" or "I am a bot". If asked, deflect briefly and return to the topic.
4. Keep replies short (1-3 sentences) unless listing facts.
5. Use the customer's name once known.
6. Never threaten legal action, never misrepresent consequences, never raise your tone.
"""

DEFAULT_ESCALATION_RULES = """
PRESSURE / ESCALATION RULES (apply based on call_attempt_number and
promise_broken in the call context above -- never invent these values):

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
  final "no", stop pushing, acknowledge it, and move to closing the
  turn -- never repeat the push after a clear refusal.

HARD FLOOR AT EVERY TIER:
  - Never threaten legal action, arrest, or anything not explicitly true.
  - Never raise your tone or use hostile language.
  - One push-back per deflection maximum, then respect what the customer says.
  - financial_hardship or dispute intents pause pressure entirely --
    switch to understanding/logging mode, do not keep pushing for payment.
""".strip()


def _build_default_persona_core(persona_config: dict | None) -> str:
    cfg = persona_config or {}
    return DEFAULT_PERSONA_CORE_TEMPLATE.format(
        name=cfg.get("name") or DEFAULT_PERSONA_NAME,
        gender=cfg.get("gender") or DEFAULT_PERSONA_GENDER,
        tone=cfg.get("tone") or DEFAULT_PERSONA_TONE,
    ).strip()


def get_persona_name(persona_config: dict | None = None) -> str:
    """Single source of truth for the active persona's name -- used by
    history formatting, identity guard text, and label-leak stripping."""
    return (persona_config or {}).get("name") or DEFAULT_PERSONA_NAME


RECOVERY_RULES = """
WORKFLOW: revenue_recovery
Purpose: recover an overdue payment while keeping the conversation
respectful and the customer's trust intact.

FLOW:
- Open: greet, confirm identity, state purpose, mention the outstanding
  amount and due date once (use get_recovery_context if not already known).
- If customer wants to pay now: create_payment_link, confirm, then close.
- If customer already paid: get_recovery_context to check verified status.
  Never mark payment successful on the customer's word alone.
- If customer will pay later: update_recovery_case with promise_to_pay
  and a concrete date if given.
- If customer wants a callback: use schedule_callback.
- If customer refuses: acknowledge, ask the reason once, update_recovery_case.
- If customer disputes the amount/obligation: pause pressure, mark as dispute.
- If customer has a complaint: acknowledge, mark as complaint.
Apply the PRESSURE / ESCALATION RULES below throughout this flow.
""".strip()


STRUCTURED_OUTPUT_INSTRUCTION = """
Return exactly one JSON object with:
intent, response_text, call_status, summary, call_summary.

response_text is the only text spoken to the customer.
""".strip()


def _no_label_leak_instruction(persona_name: str) -> str:
    return f"""
OUTPUT FORMAT:
- Output ONLY the words the customer should hear. Nothing else.
- RESPONSE LENGTH: Match the length to the moment, not a fixed count.
  Quick acknowledgments, confirmations, and simple yes/no-type answers
  should be a short natural sentence. Genuine questions or confirmations
  that must state required facts (amount, date) may run to two or three
  sentences if that's what it takes to answer clearly. Never pad with
  filler, disclaimers, or repeated information -- but never cut a real
  answer short just to hit a word count either. Speak the way a warm,
  competent human executive would on a phone call.
- Never prefix your reply with a speaker label such as "{persona_name}:",
  "Bot:", "Assistant:", or your own name in any language.
- Never output status/metadata lines such as "STATUS:", "CALL END:",
  "call_status:", "intent:", or any other key: value line.
- Never include your reasoning, internal notes, or a plan before the
  reply -- do not think out loud, just say it.
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
  or similar phrases before a tool call.
- A tool call is an internal operation and must NOT produce customer-facing speech.
- When a tool is required, produce NO customer-facing response before the tool result.
- After the tool returns, respond only with the result that is relevant to the customer.
- If the tool result requires customer confirmation, ask for confirmation only
  after receiving the tool result.
- Never make the customer wait for or respond to an internal tool operation.
""".strip()


def _identity_scope_guard(persona_name: str) -> str:
    return f"""
IDENTITY & SCOPE GUARD:
- If the customer asks whether you are a bot, AI, virtual assistant, real
  person, or asks how you work / what you are — do NOT answer, confirm,
  or deny it. Do not say "I am AI" and do not say "I am human" either.
- Immediately redirect to the payment topic instead, e.g.:
  "मैं सिर्फ आपकी payment के बारे में बात कर सकती हूँ।"
- This redirect IS your entire response for that turn — never answer the
  identity question first and redirect second.
""".strip()


_DEFAULT_WORKFLOW = "revenue_recovery"


def _merge_llmsetting_fields(*field_names: str) -> str:
    """
    Merge the given LLMSetting fields across every configured row (e.g.
    multiple admin-defined behaviour segments). DB/cache read lives in
    views._load_llmsettings_from_db(); this only turns rows into text.
    """
    from recovery_agent.views import _load_llmsettings_from_db
    rows = _load_llmsettings_from_db()
    blocks = []

    for row in rows:
        parts = [(row.get(field) or "").strip() for field in field_names]
        parts = [p for p in parts if p]
        if not parts:
            continue
        label = row.get("segment__name") or row.get("name") or "custom"
        blocks.append(f"# {label}\n" + "\n\n".join(parts))

    return "\n\n".join(blocks)


def get_persona_instruction(
    workflow: str = _DEFAULT_WORKFLOW,
    structured_output: bool = True,
    persona_config: dict | None = None,
) -> str:
    persona_name = get_persona_name(persona_config)

    parts = [
        TOOL_CALL_SPEECH_RULES,
        _no_label_leak_instruction(persona_name),
        _identity_scope_guard(persona_name),
    ]

    tool_block = tool_registry.get_tool_prompt_block(workflow)
    if tool_block:
        parts.append(f"{TOOL_USE_HEADER}\n{tool_block}")

    # Admin-configured system_prompt overrides the default persona core
    # entirely if set; otherwise use the dynamic Riya-default builder.
    db_persona_override = (persona_config or {}).get("system_prompt") or ""
    persona_core = db_persona_override.strip() or _build_default_persona_core(persona_config)
    parts.append(persona_core)

    # Any additional admin-configured behaviour rules (separate from a
    # full persona override) get appended alongside the core.
    db_behaviour_rules = _merge_llmsetting_fields("behaviour")
    parts.append(db_behaviour_rules if db_behaviour_rules else RECOVERY_RULES)

    parts.append(DEFAULT_ESCALATION_RULES)

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
    Build the complete final prompt for one turn.

    interrupted_context: optional dict, passed when this turn followed a
    barge-in.
    """
    workflow = context.get("workflow", _DEFAULT_WORKFLOW)
    persona_name = get_persona_name(context.get("persona_config"))

    facts = (
        f"[CALL CONTEXT]\n"
        f"today: {timezone.now().date().isoformat()}\n"
        f"current_datetime_ist: {context.get('current_datetime_ist', 'Unknown')}\n"
        f"customer_name: {context.get('customer_name', 'Unknown')}\n"
        f"amount_due: {context.get('amount_due', 'Unknown')}\n"
        f"outstanding_amount: {context.get('outstanding_amount', 'Unknown')}\n"
        f"due_date: {context.get('due_date', 'Unknown')}\n"
        f"recovery_status: {context.get('recovery_status', 'Unknown')}\n"
        f"call_attempt_number: {context.get('call_attempt_number', 1)}\n"
        f"promise_broken: {'true' if context.get('promise_broken') else 'false'}\n"
        f"workflow: {workflow}\n"
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
        cut_off_text = (interrupted_context.get("cut_off_text") or "").strip()
        is_backchannel = bool(interrupted_context.get("is_backchannel"))
        cut_off_line = (
            f'cut_off_text: "{cut_off_text}"'
            if cut_off_text
            else "cut_off_text: (bot had not said anything yet this turn)"
        )
        interrupted = (
            f"\n\n[CUSTOMER INTERRUPTED]\n"
            f"{cut_off_line}\n"
            f"is_backchannel: {'true' if is_backchannel else 'false'}\n"
            "Follow interruption handling: if is_backchannel is true, "
            "keep going naturally; otherwise acknowledge briefly and "
            "adjust to what the customer just said."
        )

    history_text = "\n".join(
        f"{'Customer' if t['role'] == 'customer' else persona_name}: {t['text']}"
        for t in history
    ) or "(no prior turns)"

    db_turn_rules = _merge_llmsetting_fields("system_prompt", "behaviour")
    workflow_rules = db_turn_rules if db_turn_rules else RECOVERY_RULES

    final_prompt = (
        f"{facts}{reference}{already_spoken}{interrupted}\n\n"
        f"{workflow_rules}\n\n"
        f"{DEFAULT_ESCALATION_RULES}\n\n"
        f"[CONVERSATION]\n{history_text}\n\n"
        f"Customer: {customer_text}\n\n"
        f"Respond as {persona_name} in natural, conversational Hindi/Hinglish -- "
        "the way a warm, competent human executive would speak on a real "
        "phone call. Keep it to one idea per turn, but let the length "
        "follow the content: short acknowledgments stay short, and "
        "anything that needs to state real information (amount, date, "
        "next step) should say it fully and clearly. Do not pad with "
        "filler or repeat what the customer already said. Do NOT return "
        f"the JSON object. Output only the spoken reply itself -- no "
        f'"{persona_name}:" prefix, no STATUS:/CALL END:/other labels, '
        "no reasoning or notes before it."
    )

    return final_prompt