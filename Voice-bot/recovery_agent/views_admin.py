"""
Admin / dashboard control APIs for RecoverAI. Handles:
- AI persona (LLM setting)
- Voice config (TTS voices)
- Recovery campaigns / cases / callbacks
- Call recordings / call history
- Customers
- Recovery dashboard / analytics
- Barge-in settings

Rewritten against the real 13-model schema. No Dealer / Branch / Segment /
Vehicle anywhere — those models don't exist. LLMSetting is a flat row
(no `segment` FK) selected by `is_active`, not by segment name.

Also defines the two sync helpers consumers.py imports and calls but that
did not exist anywhere in the previous version of this file:
    _resolve_customer_sync   (replaces the old _resolve_dealer_branch_sync)
    _persist_recording_paths_sync
"""
import json
import logging
import os
import mimetypes

from django.http import JsonResponse, FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.conf import settings
from django.db.models import Count, Q, Sum, Avg
from django.utils import timezone

from .models import (
    Customer, CallSession, TTSVoice, LLMSetting,
    RecoveryCase, RecoveryCampaign, Callback, PaymentRecord,
)
from .views import invalidate_module_rules_cache

logger = logging.getLogger('recovery_agent')

BARGE_IN_THRESHOLD_MIN_RMS = 700
BARGE_IN_THRESHOLD_MAX_RMS = 2200
BARGE_IN_DEFAULT_ENABLED = True
BARGE_IN_DEFAULT_RMS = 900


# ═══════════════════════════════════════════════════════════════
# SERIALIZERS
# ═══════════════════════════════════════════════════════════════

def _serialize_tts_voice(voice):
    if voice is None:
        return None
    return {
        "id": voice.id,
        "voice_name": voice.voice_name,
        "gender": voice.gender,
        "provider_voice_id": voice.provider_voice_id,
        "provider_name": voice.provider_name,
        "language": voice.language,
        "is_active": voice.is_active,
        "sample_url": voice.sample_url,
        "created_at": voice.created_at.isoformat() if voice.created_at else None,
        "updated_at": voice.updated_at.isoformat() if voice.updated_at else None,
    }


def _serialize_llm_setting(setting):
    if setting is None:
        return None
    return {
        "id": setting.id,
        "name": setting.name,
        "is_active": setting.is_active,
        "provider": setting.provider,
        "model": setting.model,
        "temperature": setting.temperature,
        "max_tokens": setting.max_tokens,
        "persona_name": setting.persona_name,
        "opening_line": setting.opening_line,
        "system_prompt": setting.system_prompt,
        "behaviour": setting.behaviour,
        "voice": _serialize_tts_voice(setting.voice),
        "tone": setting.tone,
        "pace": setting.pace,
        "barge_in_threshold": setting.barge_in_threshold,
        "max_turns": setting.max_turns,
        "allow_customer_barge_in": setting.allow_customer_barge_in,
        "language": setting.language,
        "response_max_chars": setting.response_max_chars,
        "questions_per_turn_max": setting.questions_per_turn_max,
        "created_at": setting.created_at.isoformat() if setting.created_at else None,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


def _serialize_customer_brief(customer):
    if customer is None:
        return None
    return {
        "id": customer.id,
        "name": customer.name,
        "phone_number": customer.phone_number,
    }


def _serialize_recording(session):
    """Serializes a CallSession row as a 'recording' for the frontend."""
    return {
        "id": session.id,
        "session_id": str(session.session_id),
        "customer": _serialize_customer_brief(session.customer),
        "campaign_id": session.campaign_id,
        "agent": _serialize_llm_setting_brief(session.agent),
        "status": session.status,
        "intent": session.intent,
        "recovery_outcome": session.recovery_outcome,
        "direction": session.direction,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": session.duration_seconds,
        "transcript": session.transcript,
        "intent_history": session.intent_history,
        "call_summary": session.call_summary,
        "recording_stereo": session.recording_stereo,
        "recording_mixed": session.recording_mixed,
    }


def _serialize_llm_setting_brief(setting):
    if setting is None:
        return None
    return {
        "id": setting.id,
        "persona_name": setting.persona_name,
        "name": setting.name,
    }


def _serialize_callback(cb):
    return {
        "id": cb.id,
        "customer_id": cb.customer_id,
        "recovery_case_id": cb.recovery_case_id,
        "scheduled_for": cb.scheduled_for.isoformat() if cb.scheduled_for else None,
        "reason": cb.reason,
        "status": cb.status,
        "session_id": cb.session.session_id if cb.session_id else None,
        "created_at": cb.created_at.isoformat() if cb.created_at else None,
    }


def _serialize_recovery_case(case):
    return {
        "id": case.id,
        "customer_id": case.customer_id,
        "campaign_id": case.campaign_id,
        "status": case.status,
        "priority": case.priority,
        "outcome": case.outcome,
        "current_intent": case.current_intent,
        "current_outcome": case.current_outcome,
        "amount_due": str(case.amount_due),
        "amount_recovered": str(case.amount_recovered),
        "due_date": case.due_date.isoformat() if case.due_date else None,
        "promise_date": case.promise_date.isoformat() if case.promise_date else None,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
    }


# ═══════════════════════════════════════════════════════════════
# TTS VOICES
# ═══════════════════════════════════════════════════════════════

@api_view(["GET", "POST"])
def tts_voices(request):
    """
    GET  /api/admin/tts-voices/
    POST /api/admin/tts-voices/
    """
    if request.method == "GET":
        voices = TTSVoice.objects.filter(flag='c').order_by("voice_name")
        return Response({
            "success": True,
            "count": voices.count(),
            "voices": [_serialize_tts_voice(v) for v in voices],
        })

    data = request.data
    for field in ("voice_name", "gender"):
        if not data.get(field):
            return Response({"success": False, "error": f"{field} is required"}, status=400)

    voice = TTSVoice.objects.create(
        voice_name=data["voice_name"],
        gender=data["gender"],
        provider_voice_id=data.get("provider_voice_id", ""),
        provider_name=data.get("provider_name", "Murf"),
        language=data.get("language", "hi-IN"),
        is_active=data.get("is_active", True),
        sample_url=data.get("sample_url", ""),
    )
    return Response({"success": True, "voice": _serialize_tts_voice(voice)}, status=201)


@api_view(["PATCH", "DELETE"])
def tts_voice_detail(request, voice_id):
    """PATCH/DELETE /api/admin/tts-voices/<id>/"""
    try:
        voice = TTSVoice.objects.get(id=voice_id, flag='c')
    except TTSVoice.DoesNotExist:
        return Response({"success": False, "error": "voice not found"}, status=404)

    if request.method == "DELETE":
        voice.flag = 'd'
        voice.save(update_fields=["flag", "updated_at"])
        return Response({"success": True})

    data = request.data
    for field in ("voice_name", "gender", "provider_voice_id", "provider_name",
                  "language", "is_active", "sample_url"):
        if field in data:
            setattr(voice, field, data[field])
    voice.save()
    return Response({"success": True, "voice": _serialize_tts_voice(voice)})


# ═══════════════════════════════════════════════════════════════
# LLM SETTINGS (AI Persona) — single-tenant: one active row normally
# ═══════════════════════════════════════════════════════════════

@api_view(["GET", "POST"])
def llm_settings(request):
    """
    GET  /api/admin/llm-settings/
    POST /api/admin/llm-settings/
    """
    if request.method == "GET":
        qs = LLMSetting.objects.filter(flag='c').select_related("voice").order_by("-is_active", "name")
        return Response({
            "success": True,
            "count": qs.count(),
            "settings": [_serialize_llm_setting(s) for s in qs],
        })

    data = request.data
    required = ["persona_name", "voice_id", "system_prompt"]
    for field in required:
        if not data.get(field):
            return Response({"success": False, "error": f"{field} is required"}, status=400)

    try:
        voice = TTSVoice.objects.get(id=data["voice_id"], is_active=True, flag='c')
    except TTSVoice.DoesNotExist:
        return Response({"success": False, "error": "active TTS voice not found"}, status=404)

    setting = LLMSetting.objects.create(
        name=data.get("name", "default"),
        is_active=data.get("is_active", True),
        provider=data.get("provider", "gemini"),
        model=data.get("model", "gemini-2.5-flash-lite"),
        temperature=data.get("temperature", 0.4),
        max_tokens=data.get("max_tokens", 1000),
        persona_name=data["persona_name"],
        opening_line=data.get("opening_line", ""),
        system_prompt=data["system_prompt"],
        behaviour=data.get("behaviour", ""),
        voice=voice,
        tone=data.get("tone", 72),
        pace=data.get("pace", 50),
        barge_in_threshold=data.get("barge_in_threshold", 65),
        max_turns=data.get("max_turns", 10),
        allow_customer_barge_in=data.get("allow_customer_barge_in", True),
        language=data.get("language", "hi-IN"),
        response_max_chars=data.get("response_max_chars", 240),
        questions_per_turn_max=data.get("questions_per_turn_max", 1),
    )
    setting = LLMSetting.objects.select_related("voice").get(pk=setting.pk)
    invalidate_module_rules_cache()
    return Response({"success": True, "setting": _serialize_llm_setting(setting)}, status=201)


@api_view(['GET', 'PATCH'])
def llm_setting_detail(request, setting_id):
    """GET/PATCH /api/admin/llm-settings/<setting_id>/"""
    try:
        setting = LLMSetting.objects.select_related('voice').get(pk=setting_id, flag='c')
    except LLMSetting.DoesNotExist:
        return Response({"success": False, "error": "LLM setting not found"}, status=404)

    if request.method == 'GET':
        return Response({"success": True, "setting": _serialize_llm_setting(setting)})

    data = request.data
    for field in ("name", "is_active", "provider", "model", "temperature", "max_tokens",
                  "persona_name", "opening_line", "system_prompt", "behaviour", "tone",
                  "pace", "max_turns", "allow_customer_barge_in", "barge_in_threshold",
                  "language", "response_max_chars", "questions_per_turn_max"):
        if field in data:
            setattr(setting, field, data[field])

    if "voice_id" in data:
        try:
            setting.voice = TTSVoice.objects.get(id=data["voice_id"], is_active=True, flag='c')
        except TTSVoice.DoesNotExist:
            return Response({"success": False, "error": "active TTS voice not found"}, status=404)

    setting.save()
    setting = LLMSetting.objects.select_related('voice').get(pk=setting.pk)
    invalidate_module_rules_cache()
    return Response({"success": True, "setting": _serialize_llm_setting(setting)})


# ═══════════════════════════════════════════════════════════════
# BARGE-IN HELPERS (used by consumers.py at call-connect time)
# ═══════════════════════════════════════════════════════════════

def _get_barge_in_settings_sync(setting_name=None):
    """Returns (allow_barge_in: bool, rms_threshold: int).
    Single-tenant MVP: no segment concept — resolves by `name` if given,
    else the active LLMSetting row."""
    try:
        qs = LLMSetting.objects.filter(flag='c')
        setting = (
            qs.filter(name=setting_name).first() if setting_name
            else qs.filter(is_active=True).first() or qs.first()
        )
        if not setting:
            return BARGE_IN_DEFAULT_ENABLED, BARGE_IN_DEFAULT_RMS

        allow = bool(setting.allow_customer_barge_in)
        pct = max(0, min(100, setting.barge_in_threshold)) / 100.0
        rms = int(BARGE_IN_THRESHOLD_MIN_RMS + pct * (BARGE_IN_THRESHOLD_MAX_RMS - BARGE_IN_THRESHOLD_MIN_RMS))
        return allow, rms
    except Exception as e:
        logger.error(f"[BARGE-IN] settings lookup failed: {e}")
        return BARGE_IN_DEFAULT_ENABLED, BARGE_IN_DEFAULT_RMS


# ═══════════════════════════════════════════════════════════════
# CUSTOMER / CALL RESOLUTION HELPERS
# (these did NOT exist anywhere in the previous file — consumers.py
# imported them but they were never defined, which meant the app could
# not have booted successfully before now)
# ═══════════════════════════════════════════════════════════════

def _resolve_customer_sync(phone_number=None, customer_id=None):
    """
    Resolve a Customer for an inbound/outbound call. Replaces the old
    dealer/branch resolution — RecoverAI is single-tenant, so there is no
    dealer to resolve. Returns a Customer instance or None.
    """
    if customer_id:
        return Customer.objects.filter(id=customer_id, flag='c').first()
    if phone_number:
        return Customer.objects.filter(phone_number=phone_number, flag='c').first()
    return None


def _persist_recording_paths_sync(session_id, recording_stereo=None, recording_mixed=None):
    """
    Persist recording file paths onto a CallSession once a call's audio
    has finished writing to disk. Returns the updated CallSession or None.
    """
    session = CallSession.objects.filter(session_id=session_id).first()
    if not session:
        logger.warning(f"[RECORDING] no CallSession for session_id={session_id}")
        return None

    update_fields = []
    if recording_stereo is not None:
        session.recording_stereo = recording_stereo
        update_fields.append("recording_stereo")
    if recording_mixed is not None:
        session.recording_mixed = recording_mixed
        update_fields.append("recording_mixed")

    if update_fields:
        update_fields.append("updated_at")
        session.save(update_fields=update_fields)
    return session


# ═══════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def customers_list(request):
    """GET /api/admin/customers/?search=&page=1&page_size=25"""
    qs = Customer.objects.filter(flag='c')

    search = request.GET.get('search')
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone_number__icontains=search)
                        | Q(account_reference__icontains=search))

    qs = qs.order_by('-id')
    paginator = PageNumberPagination()
    paginator.page_size = 25
    paginator.page_size_query_param = "page_size"
    paginator.max_page_size = 200
    page = paginator.paginate_queryset(qs, request)

    data = [{
        "id": c.id,
        "name": c.name,
        "phone_number": c.phone_number,
        "account_reference": c.account_reference,
        "do_not_call": c.do_not_call,
        "total_calls": c.total_calls,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in page]

    return paginator.get_paginated_response(data)


@api_view(['GET'])
def customer_detail(request, customer_id):
    """GET /api/admin/customers/<id>/"""
    customer = get_object_or_404(Customer, id=customer_id, flag='c')
    cases = RecoveryCase.objects.filter(customer=customer, flag='c').order_by('-created_at')[:10]

    return Response({
        "success": True,
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone_number": customer.phone_number,
            "email": customer.email,
            "account_reference": customer.account_reference,
            "external_customer_id": customer.external_customer_id,
            "preferred_language": customer.preferred_language,
            "do_not_call": customer.do_not_call,
            "do_not_call_reason": customer.do_not_call_reason,
            "total_calls": customer.total_calls,
            "created_at": customer.created_at.isoformat() if customer.created_at else None,
        },
        "recovery_cases": [_serialize_recovery_case(c) for c in cases],
    })


# ═══════════════════════════════════════════════════════════════
# CALLS / RECORDINGS / TRANSCRIPTS
# ═══════════════════════════════════════════════════════════════

class _RecordingPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 500


@api_view(["GET"])
def recordings(request):
    """GET /api/admin/recordings/?page_size=25&search=&status=&intent=&outcome=&campaign_id="""
    qs = CallSession.objects.filter(flag='c').select_related("customer").order_by("-started_at")

    search = request.GET.get("search")
    if search:
        qs = qs.filter(
            Q(customer__name__icontains=search) |
            Q(customer__phone_number__icontains=search)
        )

    status_param = request.GET.get("status")
    if status_param:
        qs = qs.filter(status=status_param)

    intent_param = request.GET.get("intent")
    if intent_param:
        qs = qs.filter(intent=intent_param)

    outcome_param = request.GET.get("outcome")
    if outcome_param:
        qs = qs.filter(recovery_outcome=outcome_param)

    campaign_id = request.GET.get("campaign_id")
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)

    paginator = _RecordingPagination()
    page = paginator.paginate_queryset(qs, request)
    data = [_serialize_recording(s) for s in page]

    return paginator.get_paginated_response(data)


@api_view(["GET"])
def recording_audio(request, session_id):
    """GET /api/admin/recordings/<session_id>/audio/ — streams the file from disk."""
    session = get_object_or_404(CallSession, session_id=session_id, flag='c')

    path = session.recording_mixed or session.recording_stereo
    if not path:
        raise Http404("No recording file for this session")

    if not os.path.isabs(path):
        path = os.path.join(settings.MEDIA_ROOT, path)

    if not os.path.exists(path):
        raise Http404("Recording file not found on disk")

    content_type, _ = mimetypes.guess_type(path)

    return FileResponse(
        open(path, "rb"),
        content_type=content_type or "audio/mpeg",
        filename=os.path.basename(path),
    )


@api_view(["GET"])
def call_detail_admin(request, session_id):
    """GET /api/admin/calls/<session_id>/ — full call detail."""
    session = get_object_or_404(
        CallSession.objects.select_related("customer"),
        session_id=session_id, flag='c',
    )
    turns = session.turns.filter(flag='c').order_by('timestamp')

    return Response({
        "success": True,
        "call": {
            "session_id": session.session_id,
            "customer": _serialize_customer_brief(session.customer),
            "status": session.status,
            "intent": session.intent,
            "recovery_outcome": session.recovery_outcome,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            "duration_seconds": session.duration_seconds,
            "transcript": session.transcript,
            "intent_history": session.intent_history,
            "call_summary": session.call_summary,
            "recording_mixed": session.recording_mixed,
            "stt_pricing": str(session.stt_pricing or 0),
            "tts_pricing": str(session.tts_pricing or 0),
            "llm_pricing": str(session.llm_pricing or 0),
            "dialer_pricing": str(session.dialer_pricing or 0),
            "total_cost": str(session.total_cost or 0),
            "turns": [
                {
                    "id": t.id,
                    "speaker": t.speaker,
                    "text": t.text,
                    "intent": t.intent,
                    "confidence": t.confidence,
                    "at": t.timestamp.isoformat() if t.timestamp else None,
                }
                for t in turns
            ],
        }
    })


# ═══════════════════════════════════════════════════════════════
# RECOVERY DASHBOARD / ANALYTICS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def recovery_dashboard(request):
    """GET /api/admin/recovery/dashboard/?days=30&campaign_id="""
    campaign_id = request.GET.get('campaign_id')

    call_qs = CallSession.objects.filter(flag='c')
    case_qs = RecoveryCase.objects.filter(flag='c')

    if campaign_id:
        call_qs = call_qs.filter(campaign_id=campaign_id)
        case_qs = case_qs.filter(campaign_id=campaign_id)

    days = int(request.GET.get('days', 30))
    since = timezone.now() - timezone.timedelta(days=days)
    call_qs_window = call_qs.filter(started_at__gte=since)

    totals = call_qs.aggregate(total_calls=Count('id'))
    windowed = call_qs_window.aggregate(calls_attempted=Count('id'))

    connected = call_qs_window.filter(status__in=['completed', 'ongoing']).count()

    by_intent = call_qs_window.values('intent').annotate(count=Count('id')).order_by('-count')
    by_outcome = call_qs_window.values('recovery_outcome').annotate(count=Count('id')).order_by('-count')

    complaints = call_qs_window.filter(intent='complaint').count()
    callbacks = call_qs_window.filter(intent='callback_requested').count()
    declines = call_qs_window.filter(intent__in=['refused_to_pay', 'not_interested']).count()
    wrong_numbers = call_qs_window.filter(intent='wrong_number').count()

    avg_duration = call_qs_window.filter(
        duration_seconds__isnull=False
    ).aggregate(avg=Avg('duration_seconds'))['avg'] or 0

    cost_agg = call_qs_window.aggregate(
        total_stt=Sum('stt_pricing'),
        total_tts=Sum('tts_pricing'),
        total_llm=Sum('llm_pricing'),
        total_dialer=Sum('dialer_pricing'),
        total_cost=Sum('total_cost'),
    )

    recovery_value = case_qs.filter(
        status='closed', closed_at__gte=since,
    ).aggregate(total_recovered=Sum('amount_recovered'))

    return Response({
        "success": True,
        "period_days": days,
        "totals": {
            "total_calls": totals['total_calls'] or 0,
            "calls_attempted": windowed['calls_attempted'] or 0,
            "calls_connected": connected,
            "connection_rate": (
                round(connected / windowed['calls_attempted'] * 100, 2)
                if windowed['calls_attempted'] else 0
            ),
            "complaints": complaints,
            "callbacks": callbacks,
            "declines": declines,
            "wrong_numbers": wrong_numbers,
            "avg_duration_seconds": round(avg_duration, 1),
        },
        "by_intent": [{"intent": r["intent"], "count": r["count"]} for r in by_intent if r["intent"]],
        "by_outcome": [{"outcome": r["recovery_outcome"], "count": r["count"]} for r in by_outcome if r["recovery_outcome"]],
        "costs": {
            "stt": str(cost_agg['total_stt'] or 0),
            "tts": str(cost_agg['total_tts'] or 0),
            "llm": str(cost_agg['total_llm'] or 0),
            "dialer": str(cost_agg['total_dialer'] or 0),
            "total": str(cost_agg['total_cost'] or 0),
        },
        "recovery": {
            "amount_recovered": str(recovery_value['total_recovered'] or 0),
        },
    })


@api_view(['GET'])
def recovery_callbacks(request):
    """GET /api/admin/recovery/callbacks/?status=requested"""
    qs = Callback.objects.filter(flag='c').select_related('customer', 'session').order_by('-scheduled_for')

    status_param = request.GET.get('status')
    if status_param:
        qs = qs.filter(status=status_param)

    return Response({
        "success": True,
        "count": qs.count(),
        "callbacks": [_serialize_callback(cb) for cb in qs[:100]],
    })


@api_view(['GET'])
def recovery_cases(request):
    """GET /api/admin/recovery/cases/?status=open&campaign_id="""
    qs = RecoveryCase.objects.filter(flag='c').select_related('customer').order_by('-created_at')

    campaign_id = request.GET.get('campaign_id')
    if campaign_id:
        qs = qs.filter(campaign_id=campaign_id)

    status_param = request.GET.get('status')
    if status_param:
        qs = qs.filter(status=status_param)

    return Response({
        "success": True,
        "count": qs.count(),
        "cases": [_serialize_recovery_case(c) for c in qs[:100]],
    })


# ═══════════════════════════════════════════════════════════════
# RECOVERY CAMPAIGNS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET', 'POST'])
def campaigns(request):
    """
    GET  /api/admin/campaigns/
    POST /api/admin/campaigns/
    """
    if request.method == 'GET':
        qs = RecoveryCampaign.objects.filter(flag='c').order_by('-created_at')
        status_param = request.GET.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        return Response({
            "success": True,
            "count": qs.count(),
            "campaigns": [
                {
                    "id": c.id,
                    "name": c.name,
                    "campaign_type": c.campaign_type,
                    "status": c.status,
                    "customer_count": c.customer_count,
                    "calls_attempted": c.calls_attempted,
                    "cases_recovered": c.cases_recovered,
                    "amount_recovered": str(c.amount_recovered),
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "started_at": c.started_at.isoformat() if c.started_at else None,
                }
                for c in qs
            ],
        })

    data = request.data
    name = data.get('name')
    if not name:
        return Response({"success": False, "error": "name required"}, status=400)

    campaign = RecoveryCampaign.objects.create(
        name=name,
        campaign_type=data.get('campaign_type', 'payment'),
        description=data.get('description', ''),
        status='draft',
        target_due_within_days=data.get('target_due_within_days', 14),
    )
    return Response({
        "success": True,
        "campaign": {"id": campaign.id, "name": campaign.name, "status": campaign.status},
    }, status=201)


@api_view(['GET', 'PATCH'])
def campaign_detail(request, campaign_id):
    """GET/PATCH /api/admin/campaigns/<id>/"""
    try:
        campaign = RecoveryCampaign.objects.get(id=campaign_id, flag='c')
    except RecoveryCampaign.DoesNotExist:
        return Response({"success": False, "error": "not found"}, status=404)

    if request.method == 'GET':
        return Response({"success": True, "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "campaign_type": campaign.campaign_type,
            "description": campaign.description,
            "status": campaign.status,
            "customer_count": campaign.customer_count,
            "calls_attempted": campaign.calls_attempted,
            "calls_connected": campaign.calls_connected,
            "cases_recovered": campaign.cases_recovered,
            "amount_recovered": str(campaign.amount_recovered),
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
            "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
            "finished_at": campaign.finished_at.isoformat() if campaign.finished_at else None,
        }})

    data = request.data
    for field in ('name', 'status', 'campaign_type', 'description', 'target_due_within_days'):
        if field in data:
            setattr(campaign, field, data[field])
    campaign.save()
    return Response({"success": True})


# ═══════════════════════════════════════════════════════════════
# TTS TEST (admin only)
# ═══════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def admin_test_tts(request):
    """POST /api/admin/test-tts/"""
    try:
        data = json.loads(request.body)
        text = data.get('text', 'Namaste, main aapki madad karne ke liye yahan hoon.')
        provider = data.get('provider', 'google')
        voice = data.get('voice', 'hi-IN-Wavenet-A')

        from .services.tts_service import get_tts_service
        tts = get_tts_service()
        audio_bytes = tts.synthesize(text=text, provider=provider, voice_name=voice)

        import base64
        return JsonResponse({
            "success": True,
            "audio_b64": base64.b64encode(audio_bytes).decode(),
            "provider": provider,
            "voice": voice,
        })

    except Exception as e:
        logger.error(f"TTS test error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@api_view(['GET'])
def get_voice_options(request):
    """GET /api/admin/voice-options/ — available TTS provider/voice matrix"""
    return Response(settings.TTS_PROVIDERS)