"""
Admin / dashboard control APIs for RecoverAI. Handles:
- AI persona (LLM setting)
- Voice config
- LLM configuration
- Dealer / branch config
- Recovery campaigns
- Call recordings
- Call history
- Customers
- Recovery dashboard
- Analytics
- Barge-in settings

NO booking, NO test pages, NO LLM playground, NO debug endpoints. """
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
from decimal import Decimal

from .models import (
    Dealer, Branch, Customer, CallSession, TTSVoice, Segment, LLMSetting,
    Vehicle, RecoveryCase, Callback, PaymentRecord,
)

logger = logging.getLogger('voice_bot')

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
        "provider_id": voice.provider_id,
        "provider_name": voice.provider_name,
        "is_active": voice.is_active,
        "created_at": voice.created_at.isoformat() if voice.created_at else None,
        "updated_at": voice.updated_at.isoformat() if voice.updated_at else None,
    }


def _serialize_segment(segment):
    if segment is None:
        return None
    return {
        "id": segment.id,
        "name": segment.name,
        "description": segment.description,
        "module": getattr(segment, 'module', None),
        "created_at": segment.created_at.isoformat() if segment.created_at else None,
        "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
    }


def _serialize_llm_setting(setting):
    if setting is None:
        return None
    return {
        "id": setting.id,
        "dealer_id": setting.dealer_id,
        "module": setting.module,
        "segment": _serialize_segment(setting.segment),
        "persona_name": setting.persona_name,
        "agent_name": getattr(setting, 'agent_name', None),
        "opening_line": setting.opening_line,
        "system_prompt": setting.system_prompt,
        "behaviour": setting.behaviour,
        "voice": _serialize_tts_voice(setting.voice),
        "tone": setting.tone,
        "pace": setting.pace,
        "barge_in_threshold": setting.barge_in_threshold,
        "max_turns": setting.max_turns,
        "allow_customer_barge_in": setting.allow_customer_barge_in,
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


def _serialize_vehicle_brief(vehicle):
    if vehicle is None:
        return None
    return {
        "id": vehicle.id,
        "vehicle_name": vehicle.vehicle_name,
        "vehicle_model": vehicle.vehicle_model,
        "registration_no": vehicle.registration_no,
    }


def _serialize_recording(session):
    """Serializes a CallSession row as a 'recording' for the frontend."""
    return {
        "id": session.id,
        "session_id": str(session.session_id),
        "customer": _serialize_customer_brief(session.customer),
        "vehicle": _serialize_vehicle_brief(getattr(session, 'vehicle', None)),
        "segment": _serialize_segment(getattr(session, 'segment', None)),
        "agent": _serialize_llm_setting_brief(getattr(session, 'agent', None)),
        "status": session.status,
        "intent": getattr(session, 'intent', None),
        "recovery_outcome": getattr(session, 'recovery_outcome', None),
        "direction": getattr(session, 'direction', 'outbound'),
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": session.duration_seconds,
        "transcript": session.transcript,
        "intent_history": getattr(session, 'intent_history', []),
        "call_summary": getattr(session, 'call_summary', None),
        "recording_stereo": getattr(session, 'recording_stereo', None),
        "recording_mixed": getattr(session, 'recording_mixed', None),
    }


def _serialize_llm_setting_brief(setting):
    if setting is None:
        return None
    return {
        "id": setting.id,
        "persona_name": setting.persona_name,
        "agent_name": getattr(setting, 'agent_name', None),
        "module": getattr(setting, 'module', None),
    }


def _serialize_callback(cb):
    return {
        "id": cb.id,
        "customer_id": cb.customer_id,
        "scheduled_for": cb.scheduled_for.isoformat() if cb.scheduled_for else None,
        "reason": cb.reason,
        "status": cb.status,
        "session_id": str(cb.session_id) if cb.session_id else None,
        "created_at": cb.created_at.isoformat() if cb.created_at else None,
    }


def _serialize_recovery_case(case):
    return {
        "id": case.id,
        "customer_id": case.customer_id,
        "campaign_id": case.campaign_id,
        "status": case.status,
        "outcome": getattr(case, 'outcome', None),
        "amount_due": str(case.amount_due) if getattr(case, 'amount_due', None) else None,
        "amount_recovered": str(case.amount_recovered) if getattr(case, 'amount_recovered', None) else None,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "closed_at": case.closed_at.isoformat() if getattr(case, 'closed_at', None) else None,
    }


# ═══════════════════════════════════════════════════════════════
# DEALERS / BRANCHES
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def dealers_list(request):
    """GET /api/admin/dealers/"""
    ds = Dealer.objects.filter(flag='c', is_active=True).order_by('name')
    return Response({
        "success": True,
        "dealers": [
            {"id": d.id, "name": d.name, "code": getattr(d, 'code', '')}
            for d in ds
        ],
    })


@api_view(['GET'])
def dealer_branches(request):
    """GET /api/admin/branches/?dealer_id=1"""
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return Response({"success": False, "error": "dealer_id is required"}, status=400)
    branches = Branch.objects.filter(dealer_id=dealer_id, flag='c', is_active=True).order_by('name')
    return Response({
        "success": True,
        "branches": [{"id": b.id, "name": b.name} for b in branches],
    })


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
        voices = TTSVoice.objects.all().order_by("voice_name")
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
        provider_id=data.get("provider_id", 1),
        provider_name=data.get("provider_name", "Murf"),
        is_active=data.get("is_active", True),
    )
    return Response({"success": True, "voice": _serialize_tts_voice(voice)}, status=201)


@api_view(["PATCH", "DELETE"])
def tts_voice_detail(request, voice_id):
    """PATCH/DELETE /api/admin/tts-voices/<id>/"""
    try:
        voice = TTSVoice.objects.get(id=voice_id)
    except TTSVoice.DoesNotExist:
        return Response({"success": False, "error": "voice not found"}, status=404)

    if request.method == "DELETE":
        voice.delete()
        return Response({"success": True})

    data = request.data
    for field in ("voice_name", "gender", "provider_id", "provider_name", "is_active"):
        if field in data:
            setattr(voice, field, data[field])
    voice.save()
    return Response({"success": True, "voice": _serialize_tts_voice(voice)})


# ═══════════════════════════════════════════════════════════════
# SEGMENTS
# ═══════════════════════════════════════════════════════════════

@api_view(["GET", "POST"])
def segments(request):
    """
    GET  /api/admin/segments/
    POST /api/admin/segments/
    """
    if request.method == "GET":
        qs = Segment.objects.all().order_by("name")
        return Response({
            "success": True,
            "count": qs.count(),
            "segments": [_serialize_segment(s) for s in qs],
        })

    data = request.data
    name = data.get("name")
    if not name:
        return Response({"success": False, "error": "name is required"}, status=400)

    if Segment.objects.filter(name=name).exists():
        return Response({"success": False, "error": "segment already exists"}, status=409)

    seg = Segment.objects.create(
        name=name,
        description=data.get("description"),
        module=data.get("module"),
    )
    return Response({"success": True, "segment": _serialize_segment(seg)}, status=201)


# ═══════════════════════════════════════════════════════════════
# LLM SETTINGS (AI Persona)
# ═══════════════════════════════════════════════════════════════

@api_view(["GET", "POST"])
def llm_settings(request):
    """
    GET  /api/admin/llm-settings/
    POST /api/admin/llm-settings/
    """
    if request.method == "GET":
        qs = LLMSetting.objects.select_related("segment", "voice").order_by("segment__name")
        return Response({
            "success": True,
            "count": qs.count(),
            "settings": [_serialize_llm_setting(s) for s in qs],
        })

    data = request.data
    required = ["segment_id", "persona_name", "voice_id", "opening_line", "system_prompt"]
    for field in required:
        if not data.get(field):
            return Response({"success": False, "error": f"{field} is required"}, status=400)

    try:
        segment = Segment.objects.get(id=data["segment_id"])
    except Segment.DoesNotExist:
        return Response({"success": False, "error": "segment not found"}, status=404)

    try:
        voice = TTSVoice.objects.get(id=data["voice_id"], is_active=True)
    except TTSVoice.DoesNotExist:
        return Response({"success": False, "error": "active TTS voice not found"}, status=404)

    if LLMSetting.objects.filter(segment=segment).exists():
        return Response({"success": False, "error": "LLM setting already exists for this segment"}, status=409)

    setting = LLMSetting.objects.create(
        segment=segment,
        persona_name=data["persona_name"],
        agent_name=data.get("agent_name", data["persona_name"]),
        voice=voice,
        opening_line=data["opening_line"],
        system_prompt=data["system_prompt"],
        behaviour=data.get("behaviour"),
        module=data.get("module", "service_reminder"),
        tone=data.get("tone", 72),
        pace=data.get("pace", 50),
        barge_in_threshold=data.get("barge_in_threshold", 65),
        max_turns=data.get("max_turns", 10),
        allow_customer_barge_in=data.get("allow_customer_barge_in", True),
    )
    setting = LLMSetting.objects.select_related("segment", "voice").get(pk=setting.pk)
    return Response({"success": True, "setting": _serialize_llm_setting(setting)}, status=201)


@api_view(['GET', 'PATCH'])
def llm_setting_detail(request, segment_id):
    """GET/PATCH /api/admin/llm-settings/<segment_id>/"""
    try:
        setting = LLMSetting.objects.select_related('segment', 'voice').get(segment__id=segment_id)
    except LLMSetting.DoesNotExist:
        return Response({"success": False, "error": "no LLM setting for this segment"}, status=404)

    if request.method == 'GET':
        return Response({"success": True, "setting": _serialize_llm_setting(setting)})

    data = request.data
    for field in ("persona_name", "agent_name", "opening_line", "system_prompt",
                  "behaviour", "module", "tone", "pace", "max_turns",
                  "allow_customer_barge_in", "barge_in_threshold"):
        if field in data:
            setattr(setting, field, data[field])

    if "voice_id" in data:
        try:
            setting.voice = TTSVoice.objects.get(id=data["voice_id"], is_active=True)
        except TTSVoice.DoesNotExist:
            return Response({"success": False, "error": "active TTS voice not found"}, status=404)

    setting.save()
    setting = LLMSetting.objects.select_related('segment', 'voice').get(pk=setting.pk)
    return Response({"success": True, "setting": _serialize_llm_setting(setting)})


# ═══════════════════════════════════════════════════════════════
# BARGE-IN HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_barge_in_settings_sync(segment_name=None):
    """Returns (allow_barge_in: bool, rms_threshold: int)."""
    try:
        qs = LLMSetting.objects.select_related('segment')
        setting = qs.filter(segment__name=segment_name).first() if segment_name else qs.first()
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
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════

@api_view(['GET'])
def customers_list(request):
    """
    GET /api/admin/customers/?dealer_id=1&search=&page=1&page_size=25 """
    qs = Customer.objects.filter(flag='c').select_related('dealer', 'default_branch')

    dealer_id = request.GET.get('dealer_id')
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

    search = request.GET.get('search')
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone_number__icontains=search))

    paginator = PageNumberPagination()
    paginator.page_size = 25
    paginator.page_size_query_param = "page_size"
    paginator.max_page_size = 200
    page = paginator.paginate_queryset(qs, request)

    data = [{
        "id": c.id,
        "name": c.name,
        "phone_number": c.phone_number,
        "dealer": c.dealer.name if c.dealer else None,
        "branch": c.default_branch.name if c.default_branch else None,
        "do_not_call": c.do_not_call,
        "total_calls": getattr(c, 'total_calls', 0),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in page]

    return paginator.get_paginated_response(data)


@api_view(['GET'])
def customer_detail(request, customer_id):
    """GET /api/admin/customers/<id>/"""
    customer = get_object_or_404(Customer, id=customer_id, flag='c')
    vehicles = Vehicle.objects.filter(customer=customer, flag='c', is_sold_off=False)
    cases = RecoveryCase.objects.filter(customer=customer).order_by('-created_at')[:10]

    return Response({
        "success": True,
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone_number": customer.phone_number,
            "dealer": customer.dealer.name if customer.dealer else None,
            "branch": customer.default_branch.name if customer.default_branch else None,
            "do_not_call": customer.do_not_call,
            "created_at": customer.created_at.isoformat() if customer.created_at else None,
        },
        "vehicles": [_serialize_vehicle_brief(v) for v in vehicles],
        "recovery_cases": [_serialize_recovery_case(c) for c in cases],
    })


@api_view(['GET'])
def customer_vehicles(request, customer_id):
    """GET /api/admin/customers/<id>/vehicles/"""
    customer = get_object_or_404(Customer, id=customer_id, flag='c')
    vehicles = Vehicle.objects.filter(customer=customer, flag='c', is_sold_off=False)
    return Response({
        "success": True,
        "vehicles": [_serialize_vehicle_brief(v) for v in vehicles],
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
    """
    GET /api/admin/recordings/?page_size=25&search=&status=&intent=&outcome=
    """
    qs = (
        CallSession.objects
        .select_related("customer")
        .order_by("-started_at")
    )

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

    dealer_id = request.GET.get("dealer_id")
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

    paginator = _RecordingPagination()
    page = paginator.paginate_queryset(qs, request)
    data = [_serialize_recording(s) for s in page]

    return paginator.get_paginated_response(data)


@api_view(["GET"])
def recording_audio(request, session_id):
    """
    GET /api/admin/recordings/<session_id>/audio/
    Streams the recording file from disk. """
    session = get_object_or_404(CallSession, pk=session_id)

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
    """
    GET /api/admin/calls/<session_id>/
    Full call detail with transcript, intent history, summary, outcome. """
    session = get_object_or_404(
        CallSession.objects.select_related("customer"),
        session_id=session_id,
    )
    turns = session.turns.filter(flag='c').order_by('timestamp')

    return Response({
        "success": True,
        "call": {
            "session_id": str(session.session_id),
            "customer": _serialize_customer_brief(session.customer),
            "status": session.status,
            "intent": session.intent,
            "recovery_outcome": getattr(session, 'recovery_outcome', None),
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            "duration_seconds": session.duration_seconds,
            "transcript": session.transcript,
            "intent_history": getattr(session, 'intent_history', []),
            "call_summary": getattr(session, 'call_summary', None),
            "recording_mixed": getattr(session, 'recording_mixed', None),
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
    """
    GET /api/admin/recovery/dashboard/?dealer_id=1
    Aggregate stats for the admin dashboard. """
    dealer_id = request.GET.get('dealer_id')

    call_qs = CallSession.objects.all()
    case_qs = RecoveryCase.objects.all()
    cb_qs = Callback.objects.all()
    pay_qs = PaymentRecord.objects.all()

    if dealer_id:
        call_qs = call_qs.filter(dealer_id=dealer_id)
        case_qs = case_qs.filter(dealer_id=dealer_id)
        cb_qs = cb_qs.filter(dealer_id=dealer_id)
        pay_qs = pay_qs.filter(dealer_id=dealer_id)

    # Time window (default last 30 days)
    days = int(request.GET.get('days', 30))
    since = timezone.now() - timezone.timedelta(days=days)
    call_qs_window = call_qs.filter(started_at__gte=since)

    totals = call_qs.aggregate(
        total_calls=Count('id'),
    )
    windowed = call_qs_window.aggregate(
        calls_attempted=Count('id'),
    )

    connected = call_qs_window.filter(
        status__in=['completed', 'connected']
    ).count()

    by_intent = call_qs_window.values('intent').annotate(
        count=Count('id')
    ).order_by('-count')

    by_outcome = call_qs_window.values('recovery_outcome').annotate(
        count=Count('id')
    ).order_by('-count')

    complaints = call_qs_window.filter(intent='complaint').count()
    callbacks = call_qs_window.filter(intent='callback_requested').count()
    declines = call_qs_window.filter(intent__in=['service_declined', 'not_interested']).count()
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
        status='closed',
        closed_at__gte=since,
    ).aggregate(
        total_recovered=Sum('amount_recovered'),
    )

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
        "by_intent": [
            {"intent": row["intent"], "count": row["count"]}
            for row in by_intent if row["intent"]
        ],
        "by_outcome": [
            {"outcome": row["recovery_outcome"], "count": row["count"]}
            for row in by_outcome if row["recovery_outcome"]
        ],
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
    """
    GET /api/admin/recovery/callbacks/?dealer_id=1&status=pending
    List scheduled callbacks. """
    qs = Callback.objects.all().select_related('customer', 'session').order_by('-scheduled_for')

    dealer_id = request.GET.get('dealer_id')
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

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
    """
    GET /api/admin/recovery/cases/?dealer_id=1&status=open
    List recovery cases. """
    qs = RecoveryCase.objects.all().select_related('customer').order_by('-created_at')

    dealer_id = request.GET.get('dealer_id')
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

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
    from .models import RecoveryCampaign

    if request.method == 'GET':
        qs = RecoveryCampaign.objects.all().order_by('-created_at')
        dealer_id = request.GET.get('dealer_id')
        if dealer_id:
            qs = qs.filter(dealer_id=dealer_id)

        return Response({
            "success": True,
            "count": qs.count(),
            "campaigns": [
                {
                    "id": c.id,
                    "name": c.name,
                    "dealer_id": c.dealer_id,
                    "module": getattr(c, 'module', None),
                    "status": c.status,
                    "customer_count": getattr(c, 'customer_count', 0),
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "started_at": c.started_at.isoformat() if getattr(c, 'started_at', None) else None,
                }
                for c in qs
            ],
        })

    data = request.data
    name = data.get('name')
    dealer_id = data.get('dealer_id')
    module = data.get('module', 'service_reminder')

    if not name or not dealer_id:
        return Response({"success": False, "error": "name and dealer_id required"}, status=400)

    campaign = RecoveryCampaign.objects.create(
        name=name,
        dealer_id=dealer_id,
        module=module,
        status='draft',
    )
    return Response({
        "success": True,
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
        }
    }, status=201)


@api_view(['GET', 'PATCH'])
def campaign_detail(request, campaign_id):
    """GET/PATCH /api/admin/campaigns/<id>/"""
    from .models import RecoveryCampaign
    try:
        campaign = RecoveryCampaign.objects.get(id=campaign_id)
    except RecoveryCampaign.DoesNotExist:
        return Response({"success": False, "error": "not found"}, status=404)

    if request.method == 'GET':
        return Response({"success": True, "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "dealer_id": campaign.dealer_id,
            "module": getattr(campaign, 'module', None),
            "status": campaign.status,
            "customer_count": getattr(campaign, 'customer_count', 0),
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        }})

    data = request.data
    for field in ('name', 'status', 'module'):
        if field in data:
            setattr(campaign, field, data[field])
    campaign.save()
    return Response({"success": True})


# ═══════════════════════════════════════════════════════════════
# TTS TEST (admin only — not exposed in customer-facing routes)
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