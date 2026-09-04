"""
Views — minimal set for local curl testing. """

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

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
        from recovery_agent.services.intent_service import recovery_intent_service
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
        from recovery_agent.tools import execute_tool, set_tool_session, reset_tool_session
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
        from recovery_agent.tools import get_tool_declarations
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
# LEGACY: stub for consumers.py / views_admin.py imports
# (so old imports don't break the server)
# ═══════════════════════════════════════════════════════════════

def get_or_create_call_session(session_id, phone_number=None, **kwargs):
    """Legacy stub — full impl can come later."""
    from recovery_agent.models import CallSession, Customer
    customer = Customer.objects.filter(phone_number=phone_number, flag="c").first() if phone_number else None
    obj, _ = CallSession.objects.get_or_create(
        session_id=session_id,
        defaults={"customer": customer, "dealer_id": customer.dealer_id if customer else None},
    )
    return obj


def get_customer_context(phone_number):
    """Legacy stub — returns a context dict similar to the old format."""
    from recovery_agent.services.crm_service import crm_service
    profile = crm_service.get_customer_by_phone(phone_number) or {}
    return {
        "customer_name": profile.get("customer_name", "Customer"),
        "vehicle_model": (profile.get("vehicles") or [{}])[0].get("model", "Unknown") if profile.get("vehicles") else "Unknown",
        "due_date": (profile.get("vehicles") or [{}])[0].get("next_service_due_date") if profile.get("vehicles") else "Unknown",
        "module": "service_recovery",
        "branch": profile.get("branch_name", "Unknown"),
    }


def get_customer_context_by_phone(phone_number):
    return get_customer_context(phone_number)


def get_random_customer_context():
    from recovery_agent.models import Customer
    c = Customer.objects.filter(flag="c", do_not_call=False).order_by("?").first()
    if not c:
        return {
            "customer_name": "Customer",
            "vehicle_model": "Unknown",
            "due_date": "Unknown",
            "module": "service_recovery",
            "branch": "Unknown",
            "customer_id": None,
            "phone_number": None,
        }
    ctx = get_customer_context(c.phone_number)
    ctx["customer_id"] = c.id
    ctx["phone_number"] = c.phone_number
    return ctx


def save_turn(session_id, speaker, text, **kwargs):
    """Legacy stub — saves a ConversationTurn row."""
    from recovery_agent.models import ConversationTurn, CallSession
    sess = CallSession.objects.filter(session_id=session_id).first()
    if not sess:
        return None
    return ConversationTurn.objects.create(
        call_session=sess,
        dealer=sess.dealer,
        speaker=speaker,
        text=text,
        **{k: v for k, v in kwargs.items() if k in [
            "intent", "confidence", "filler_text", "accuracy", "filler_accuracy",
            "llm_pricing", "stt_pricing", "tts_pricing", "timing",
        ]},
    )


def end_call_session(session_id, status="completed"):
    from recovery_agent.models import CallSession
    from django.utils import timezone
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
    """Legacy stub — generates a basic summary."""
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


# Stubs for old Honda booking functions (consumers.py imports these)
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
