"""
Views — minimal set for local curl testing.
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.conf import settings

logger = logging.getLogger('recovery_agent')


# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

def health_check(request):
    """GET /api/health/ — quick server check."""
    return JsonResponse({
        "status": "ok",
        "service": "recovery_agent",
        "version": "1.0.0-local",
        "timestamp": timezone.now().isoformat(),
        "redis": _check_redis(),
    })


def _check_redis():
    try:
        from recovery_agent.services.conversation_history import _get_redis
        rc = _get_redis()
        return "available" if rc is not None else "in_memory_fallback"
    except Exception:
        return "error"


# ═══════════════════════════════════════════════════════════════
# TEST: process a turn via curl
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def test_process_turn(request):
    """
    POST /api/test/process-turn/
    Body: {
        "session_id": "test-123",
        "customer_id": 1,
        "customer_text": "Mera payment pending hai kya?"
    }
    """
    try:
        body = json.loads(request.body or "{}")
        session_id = body.get("session_id", f"test-{timezone.now().timestamp()}")
        customer_id = body.get("customer_id")
        customer_text = body.get("customer_text", "")
        history = body.get("history", [])

        if not customer_text:
            return JsonResponse({"success": False, "error": "customer_text is required"}, status=400)

        from recovery_agent.services.recovery_service import recovery_service
        result = recovery_service.process_turn(
            session_id=session_id,
            customer_text=customer_text,
            customer_id=customer_id,
            history=history,
        )
        return JsonResponse({"success": True, **result})
    except Exception as e:
        logger.exception("test_process_turn failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# TEST: classify intent only
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def test_classify_intent(request):
    """
    POST /api/test/classify-intent/
    Body: {"customer_text": "...", "history": [...]}
    """
    try:
        body = json.loads(request.body or "{}")
        customer_text = body.get("customer_text", "")
        history = body.get("history", [])
        # NOTE: the module is recovery_intent_service.py, not intent_service.py
        # (the old import path here was wrong and would ImportError).
        from recovery_agent.services.recovery_intent_service import recovery_intent_service
        result = recovery_intent_service.detect_intent(customer_text, history=history)
        return JsonResponse({"success": True, **result})
    except Exception as e:
        logger.exception("test_classify_intent failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# TEST: tool call directly
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def test_tool_call(request):
    """
    POST /api/test/tool-call/
    Body: {
        "tool": "send_payment_link",
        "arguments": {"customer_id": 1, "session_id": "test-1"}
    }
    """
    try:
        body = json.loads(request.body or "{}")
        tool_name = body.get("tool")
        arguments = body.get("arguments", {})
        if not tool_name:
            return JsonResponse({"success": False, "error": "tool name required"}, status=400)
        from recovery_agent.tools.tool_registry import (
            execute_tool, set_tool_session, reset_tool_session,
        )
        from recovery_agent.tools.recovery_tools import register_all_recovery_tools
        register_all_recovery_tools()
        session_id = arguments.get("session_id", f"test-{timezone.now().timestamp()}")
        token = set_tool_session(session_id)
        try:
            result = execute_tool(tool_name, arguments)
        finally:
            reset_tool_session(token)
        return JsonResponse({"success": True, "result": result})
    except Exception as e:
        logger.exception("test_tool_call failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# TEST: list all registered tools
# ═══════════════════════════════════════════════════════════════

@require_http_methods(["GET"])
def test_list_tools(request):
    """GET /api/test/list-tools/"""
    try:
        from recovery_agent.tools.tool_registry import get_tool_declarations
        from recovery_agent.tools.recovery_tools import register_all_recovery_tools
        register_all_recovery_tools()
        tools = get_tool_declarations()
        return JsonResponse({"success": True, "tools": tools or []})
    except Exception as e:
        logger.exception("test_list_tools failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# TEST: state snapshot
# ═══════════════════════════════════════════════════════════════

@require_http_methods(["GET"])
def test_get_state(request):
    """GET /api/test/state/?session_id=...&customer_id=..."""
    try:
        session_id = request.GET.get("session_id")
        customer_id = request.GET.get("customer_id")
        if not session_id:
            return JsonResponse({"success": False, "error": "session_id required"}, status=400)
        from recovery_agent.services.conversation_history import get_state, get_conversation_history
        state = get_state(session_id)
        history = get_conversation_history(session_id)
        profile = None
        if customer_id:
            from recovery_agent.services.crm_service import crm_service
            profile = crm_service.get_recovery_profile(int(customer_id))
        return JsonResponse({
            "success": True,
            "state": state,
            "history": history,
            "customer_profile": profile,
        })
    except Exception as e:
        logger.exception("test_get_state failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# TEST: clear session
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def test_clear_session(request):
    """POST /api/test/clear-session/  body: {session_id: "..."}"""
    try:
        body = json.loads(request.body or "{}")
        session_id = body.get("session_id")
        if not session_id:
            return JsonResponse({"success": False, "error": "session_id required"}, status=400)
        from recovery_agent.services.conversation_history import clear_state, clear_conversation
        clear_state(session_id)
        clear_conversation(session_id)
        return JsonResponse({"success": True, "cleared": session_id})
    except Exception as e:
        logger.exception("test_clear_session failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════
# Helpers used by consumers.py / views_admin.py
# Rewritten against the real 13-model schema: no dealer_id, no vehicle,
# no branch, no module. CallSession/ConversationTurn have no `dealer`
# field at all, so the old versions of get_or_create_call_session() and
# save_turn() below would have raised on first use.
# ═══════════════════════════════════════════════════════════════

def get_or_create_call_session(session_id, phone_number=None, customer_id=None, **kwargs):
    """Get or create a CallSession for this session_id."""
    from recovery_agent.models import CallSession, Customer

    customer = None
    if customer_id:
        customer = Customer.objects.filter(id=customer_id, flag="c").first()
    elif phone_number:
        customer = Customer.objects.filter(phone_number=phone_number, flag="c").first()

    obj, _ = CallSession.objects.get_or_create(
        session_id=session_id,
        defaults={"customer": customer},
    )
    return obj


def get_customer_context(phone_number):
    """Returns the LLM-facing recovery context dict for a phone number."""
    from recovery_agent.services.crm_service import crm_service
    profile = crm_service.get_customer_by_phone(phone_number) or {}
    case = profile.get("open_case") or {}
    return {
        "customer_id": profile.get("customer_id"),
        "customer_name": profile.get("customer_name", "Customer"),
        "phone_number": profile.get("phone_number", phone_number),
        "amount_due": case.get("amount_due", "0"),
        "due_date": case.get("due_date"),
        "recovery_status": case.get("status", "no_open_case"),
        "workflow": "revenue_recovery",
    }


def get_customer_context_by_phone(phone_number):
    return get_customer_context(phone_number)


def get_random_customer_context():
    from recovery_agent.models import Customer
    c = Customer.objects.filter(flag="c", do_not_call=False).order_by("?").first()
    if not c:
        return {
            "customer_name": "Customer",
            "amount_due": "0",
            "due_date": None,
            "recovery_status": "no_open_case",
            "workflow": "revenue_recovery",
            "customer_id": None,
            "phone_number": None,
        }
    ctx = get_customer_context(c.phone_number)
    ctx["customer_id"] = c.id
    ctx["phone_number"] = c.phone_number
    return ctx


def save_turn(session_id, speaker, text, **kwargs):
    """Saves a ConversationTurn row."""
    from recovery_agent.models import ConversationTurn, CallSession
    sess = CallSession.objects.filter(session_id=session_id).first()
    if not sess:
        return None
    return ConversationTurn.objects.create(
        call_session=sess,
        speaker=speaker,
        text=text,
        **{k: v for k, v in kwargs.items() if k in [
            "intent", "confidence", "entities", "filler_text", "accuracy",
            "filler_accuracy", "llm_accuracy", "llm_pricing", "stt_pricing",
            "tts_pricing", "timing",
        ]},
    )


def end_call_session(session_id, status="completed"):
    from recovery_agent.models import CallSession
    sess = CallSession.objects.filter(session_id=session_id).first()
    if not sess:
        return None
    sess.status = status
    sess.ended_at = timezone.now()
    if sess.started_at:
        sess.duration_seconds = int((sess.ended_at - sess.started_at).total_seconds())
    sess.save()
    return sess


def finalize_call_summary(session_id, context, **kwargs):
    from recovery_agent.models import CallSession
    sess = CallSession.objects.filter(session_id=session_id).first()
    if not sess:
        return None
    sess.call_summary = f"Call with {context.get('customer_name', 'Customer')} ended. Status: {sess.status}."
    sess.save()
    return sess


def get_history_for_llm(session_id):
    from recovery_agent.services.conversation_history import get_conversation_history
    history = get_conversation_history(session_id)
    return [{"role": h["speaker"].lower(), "text": h["text"]} for h in history]


def save_turn_scores(turn_id, **kwargs):
    from recovery_agent.models import ConversationTurn
    try:
        turn = ConversationTurn.objects.get(id=turn_id)
        for k, v in kwargs.items():
            if hasattr(turn, k):
                setattr(turn, k, v)
        turn.save()
        return turn
    except Exception:
        return None


def log_service_error(**kwargs):
    from recovery_agent.models import ServiceErrorLog
    try:
        return ServiceErrorLog.objects.create(**kwargs)
    except Exception as e:
        logger.error(f"log_service_error failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# Stubs for old Honda booking functions — consumers.py still imports
# these names; once consumers.py is rewritten these can be deleted
# entirely along with the import.
# ═══════════════════════════════════════════════════════════════

def extract_slot_request(text, **kwargs):
    return None, None, False


def extract_slot_continuation(text, **kwargs):
    return None, None, False


def mentions_confirmation(text):
    return False


def get_available_slots(date):
    return []


def format_slots_for_reference(date, slots):
    return ""


def book_slot_for_session(session_id, date, time):
    return {"success": False, "error": "not_implemented_in_recovery_agent"}

# ═══════════════════════════════════════════════════════════════
# LLMSetting -> prompt_builder bridge (persona/behaviour rows)
# Cached in-process; invalidated on admin write (see views_admin.py).
# ═══════════════════════════════════════════════════════════════

_llmsettings_cache = None

def _load_llmsettings_from_db():
    """Returns the active LLMSetting row(s) as plain dicts for
    prompt_builder._merge_llmsetting_fields(). Single-tenant MVP: normally
    exactly one is_active=True row; the list shape supports future A/B rows."""
    global _llmsettings_cache
    if _llmsettings_cache is not None:
        return _llmsettings_cache

    from recovery_agent.models import LLMSetting
    rows = LLMSetting.objects.filter(flag='c', is_active=True).order_by('name')
    _llmsettings_cache = [
        {
            "name": s.name,
            "persona_name": s.persona_name,
            "opening_line": s.opening_line,
            "system_prompt": s.system_prompt,
            "behaviour": s.behaviour,
        }
        for s in rows
    ]
    return _llmsettings_cache


def invalidate_module_rules_cache():
    """Call after any LLMSetting create/update/delete so the next turn
    picks up the change instead of serving the stale in-process cache."""
    global _llmsettings_cache
    _llmsettings_cache = None


def get_active_persona_config():
    """
    Builds the persona_config dict consumed by prompt_builder.get_persona_instruction()
    and build_turn_input(). Only text-prompt fields belong here -- tone/pace/voice
    are TTS delivery settings, not prompt content, and are read separately by
    the TTS/consumers layer via get_active_llm_setting_raw() below.
    """
    from recovery_agent.models import LLMSetting
    setting = LLMSetting.objects.filter(flag='c', is_active=True).order_by('name').first()
    if not setting:
        return None
    return {
        "name": setting.persona_name,
        "system_prompt": setting.system_prompt,
        "behaviour": setting.behaviour,
        "opening_line": setting.opening_line,
    }


def get_active_llm_setting_raw():
    """Full active LLMSetting row for callers that need TTS/voice/timing
    fields (consumers.py), not just prompt text."""
    from recovery_agent.models import LLMSetting
    return (
        LLMSetting.objects.select_related('voice')
        .filter(flag='c', is_active=True)
        .order_by('name')
        .first()
    )

def set_dialer_call_id(session_id, dialer_call_id):
    """Persist Plivo's streamId/call_uuid onto the CallSession row."""
    from recovery_agent.models import CallSession
    sess = CallSession.objects.filter(session_id=session_id).first()
    if not sess:
        logger.warning(f"[DIALER] no CallSession for session_id={session_id}, cannot set dialer_call_id")
        return None
    sess.dialer_call_id = dialer_call_id
    sess.save(update_fields=["dialer_call_id", "updated_at"])
    return sess

# ═══════════════════════════════════════════════════════════════
# Voice Test (demo) — persona-by-id lookups
# Same shape as get_active_persona_config / get_active_llm_setting_raw
# above, but for a SPECIFIC LLMSetting id rather than whichever row has
# is_active=True. Used only by the Voice Test admin page (consumers.py's
# demo_mode branch) so admins can preview any persona, not just the live
# one, without touching is_active.
# ═══════════════════════════════════════════════════════════════

def get_persona_config_by_id(persona_id):
    from recovery_agent.models import LLMSetting
    setting = LLMSetting.objects.filter(flag='c', id=persona_id).first()
    if not setting:
        return None
    return {
        "name": setting.persona_name,
        "system_prompt": setting.system_prompt,
        "behaviour": setting.behaviour,
        "opening_line": setting.opening_line,
    }


def get_llm_setting_raw_by_id(persona_id):
    from recovery_agent.models import LLMSetting
    return (
        LLMSetting.objects.select_related('voice')
        .filter(flag='c', id=persona_id)
        .first()
    )

def plivo_answer(request):
    """GET /api/voice/plivo/answer/?session_id=...&phone=...
    Plivo hits this the instant the call is answered. Returns XML that
    tells Plivo to open a bidirectional audio stream to our WebSocket."""
    from django.http import HttpResponse
    session_id = request.GET.get("session_id", "")
    phone = request.GET.get("phone", "")
    base = settings.PUBLIC_BASE_URL.rstrip('/')
    ws_scheme = "wss" if base.startswith("https") else "ws"
    ws_host = base.split("://", 1)[1]
    stream_url = f"{ws_scheme}://{ws_host}/api/voice/ws/plivo/{session_id}/{phone}/"

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" audioTrack="both" streamTimeout="120"
            keepCallAlive="true" contentType="audio/x-l16;rate=8000">
        {stream_url}
    </Stream>
</Response>'''
    return HttpResponse(xml, content_type="text/xml")


def plivo_hangup(request):
    """GET /api/voice/plivo/hangup/?session_id=... -- Plivo notifies here
    when the call ends from their side (busy/no-answer/failed/completed).
    The WebSocket disconnect() already handles normal call-ended cleanup;
    this only matters for calls that never even connected to the stream."""
    from django.http import HttpResponse, JsonResponse
    from recovery_agent.models import CallSession
    session_id = request.GET.get("session_id")
    hangup_cause = request.GET.get("HangupCause", "")
    if session_id:
        sess = CallSession.objects.filter(session_id=session_id).first()
        if sess and sess.status not in ("completed", "failed"):
            sess.status = "failed" if hangup_cause and hangup_cause != "NORMAL_CLEARING" else "no_answer"
            sess.save(update_fields=["status", "updated_at"])
    return JsonResponse({"ok": True})

@csrf_exempt
def trigger_call(request):
    """POST /api/admin/calls/trigger/  body: {"customer_id": 123}
    Wired to the dashboard's "Call" button."""
    from django.http import JsonResponse
    import json as _json
    from .services.plivo_service import initiate_outbound_call
    try:
        body = _json.loads(request.body or "{}")
        customer_id = body.get("customer_id")
        if not customer_id:
            return JsonResponse({"success": False, "error": "customer_id required"}, status=400)
        result = initiate_outbound_call(customer_id)
        return JsonResponse(result, status=200 if result["success"] else 400)
    except Exception as e:
        logger.exception("trigger_call failed")
        return JsonResponse({"success": False, "error": str(e)}, status=500)
    
