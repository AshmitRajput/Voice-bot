"""
Admin endpoints - Settings + Voice Config
NO separate settings_service.py - direct models use karo!

Also holds the CRM (VehicleServiceRecord/CallSession retrieval) endpoints.

SERVICE BOOKING (Appointment) endpoints and their supporting logic have
moved to views.py -- all DB-touching logic (booking, prompt-settings DB
fetch, etc.) now lives there. Import from voice_bot.views instead of here
for anything booking-related (get_available_slots, book_slot,
book_slot_for_session, extract_slot_request, extract_slot_continuation,
set_call_intent, booking_availability, create_booking, cancel_booking,
list_bookings, etc.).
"""
from django.conf import global_settings
import json
import logging
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.conf import settings
from rest_framework.pagination import PageNumberPagination
import traceback
import os
import mimetypes
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
import json
import re
from django.shortcuts import render
from .models import Dealer, Branch, Customer, CallSession, Appointment, TTSVoice, Segment, LLMSetting, Vehicle, ServiceSchedule, SERVICE_TYPE_CHOICES
from django.db.models import Count, Q
from datetime import datetime, timedelta

BARGE_IN_THRESHOLD_MIN_RMS = 700    # slider=0   -> most sensitive
BARGE_IN_THRESHOLD_MAX_RMS = 2200   # slider=100 -> least sensitive
BARGE_IN_DEFAULT_ENABLED = True
BARGE_IN_DEFAULT_RMS = 900

logger = logging.getLogger('voice_bot')


# ========== GET SETTINGS ==========

from voice_bot.services.settings_service import get_ai_settings, update_ai_settings

@api_view(['GET'])
def admin_get_settings(request):
    try:
        obj, created = Dealer.objects.get_or_create(
            pk=1,
            defaults={
                'active_prompt': 'Tum OM Honda ki AI assistant ho...',
                'tts_provider': 'google',
                'tts_voice': 'hi-IN-Wavenet-A'
            }
        )
        return Response({
            'active_prompt': obj.active_prompt,
            'tts_provider': obj.tts_provider,
            'tts_voice': obj.tts_voice,
            'updated_at': obj.updated_at
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@csrf_exempt
def admin_update_settings(request):
    
    data = request.POST  
    result = update_ai_settings(  # ✅ Function call
        active_prompt=data.get('active_prompt'),
        tts_provider=data.get('tts_provider'),
        tts_voice=data.get('tts_voice')
    )
    return JsonResponse({"success": True, "data": result})



# ========== GET VOICE OPTIONS ==========
@api_view(['GET'])
def get_voice_options(request):
    """Sare available TTS voices"""
    return Response(settings.TTS_PROVIDERS)


# ========== TEST TTS ==========
@csrf_exempt
@require_http_methods(["POST"])
def admin_test_tts(request):
    """TTS test karo - admin panel se"""
    try:
        data = json.loads(request.body)
        text = data.get('text', 'Namaste! Main Priya bol rahi hoon.')
        provider = data.get('provider', 'google')
        voice = data.get('voice', 'hi-IN-Wavenet-A')
        
        # TTS service call
        from .services.tts_service import get_tts_service
        tts = get_tts_service()
        
        audio_bytes = tts.synthesize(text=text, provider=provider, voice_name=voice)
        
        import base64
        return JsonResponse({
            "success": True,
            "audio_b64": base64.b64encode(audio_bytes).decode(),
            "provider": provider,
            "voice": voice
        })
        
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

def _serialize_tts_voice(voice: TTSVoice) -> dict:
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


def _serialize_segment(segment: Segment) -> dict:
    return {
        "id": segment.id,
        "name": segment.name,
        "description": segment.description,
        "created_at": segment.created_at.isoformat() if segment.created_at else None,
        "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
    }


def _serialize_llm_setting(setting: LLMSetting, with_metrics: bool = True) -> dict:
    data = {
        "id": setting.id,
        "dealer_id": setting.dealer_id,   # NEW
        "module": setting.module,         # NEW
        "segment": _serialize_segment(setting.segment),

        # Persona
        "persona_name": setting.persona_name,
        "opening_line": setting.opening_line,
        "system_prompt": setting.system_prompt,
        "behaviour": setting.behaviour,

        # Voice
        "voice": _serialize_tts_voice(setting.voice),

        # Conversation tuning
        "tone": setting.tone,
        "pace": setting.pace,
        "barge_in_threshold": setting.barge_in_threshold,
        "max_turns": setting.max_turns,
        "allow_customer_barge_in": setting.allow_customer_barge_in,

        "created_at": setting.created_at.isoformat() if setting.created_at else None,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }

    if with_metrics:
        # If CallSession has a segment/workflow relationship in your actual
        # model, calculate metrics here. Otherwise don't fake them.
        data.update({
            "calls": 0,
            "connect_rate": 0,
            "booking_rate": 0,
        })

    return data

# ============================================================
# TTS VOICE
# ============================================================

@api_view(["GET", "POST"])
def tts_voices(request):
    """
    GET  /api/tts-voices/
    POST /api/tts-voices/
    """

    if request.method == "GET":
        voices = TTSVoice.objects.all().order_by("voice_name")

        return Response({
            "success": True,
            "count": voices.count(),
            "voices": [
                _serialize_tts_voice(voice)
                for voice in voices
            ],
        })

    # POST
    try:
        data = request.data
    except Exception:
        return Response({
            "success": False,
            "error": "Invalid request data",
        }, status=400)

    required_fields = ["voice_name", "gender"]

    for field in required_fields:
        if not data.get(field):
            return Response({
                "success": False,
                "error": f"{field} is required",
            }, status=400)

    voice = TTSVoice.objects.create(
        voice_name=data["voice_name"],
        gender=data["gender"],
        provider_id=data.get("provider_id", 1),
        provider_name=data.get("provider_name", "Murf"),
        is_active=data.get("is_active", True),
    )

    return Response({
        "success": True,
        "voice": _serialize_tts_voice(voice),
    }, status=201)


# ============================================================
# SEGMENT
# ============================================================

@api_view(["GET", "POST"])
def segments(request):
    """
    GET  /api/segments/
    POST /api/segments/
    """

    if request.method == "GET":
        segment_qs = Segment.objects.all().order_by("name")

        return Response({
            "success": True,
            "count": segment_qs.count(),
            "segments": [
                _serialize_segment(segment)
                for segment in segment_qs
            ],
        })

    # POST
    data = request.data

    name = data.get("name")

    if not name:
        return Response({
            "success": False,
            "error": "name is required",
        }, status=400)

    if Segment.objects.filter(name=name).exists():
        return Response({
            "success": False,
            "error": "segment with this name already exists",
        }, status=409)

    segment = Segment.objects.create(
        name=name,
        description=data.get("description"),
    )

    return Response({
        "success": True,
        "segment": _serialize_segment(segment),
    }, status=201)

# ============================================================
# LLM SETTINGS
# ============================================================

@api_view(["GET", "POST"])
def llm_settings(request):
    """
    GET  /api/llm-settings/
    POST /api/llm-settings/
    """

    if request.method == "GET":
        settings_qs = (
            LLMSetting.objects
            .select_related("segment", "voice")
            .order_by("segment__name")
        )

        return Response({
            "success": True,
            "count": settings_qs.count(),
            "settings": [
                _serialize_llm_setting(setting, with_metrics=False)
                for setting in settings_qs
            ],
        })

    # POST
    data = request.data

    # --------------------------------------------------------
    # Required fields
    # --------------------------------------------------------

    required_fields = [
        "segment_id",
        "persona_name",
        "voice_id",
        "opening_line",
        "system_prompt",
    ]

    for field in required_fields:
        if data.get(field) in [None, ""]:
            return Response({
                "success": False,
                "error": f"{field} is required",
            }, status=400)

    # --------------------------------------------------------
    # Get Segment
    # --------------------------------------------------------

    try:
        segment = Segment.objects.get(
            id=data["segment_id"]
        )
    except Segment.DoesNotExist:
        return Response({
            "success": False,
            "error": "segment not found",
        }, status=404)

    # --------------------------------------------------------
    # Get Voice
    # --------------------------------------------------------

    try:
        voice = TTSVoice.objects.get(
            id=data["voice_id"],
            is_active=True,
        )
    except TTSVoice.DoesNotExist:
        return Response({
            "success": False,
            "error": "active TTS voice not found",
        }, status=404)

    # --------------------------------------------------------
    # OneToOneField protection
    # --------------------------------------------------------

    if LLMSetting.objects.filter(segment=segment).exists():
        return Response({
            "success": False,
            "error": "LLM setting already exists for this segment",
        }, status=409)

    # --------------------------------------------------------
    # Create
    # --------------------------------------------------------

    setting = LLMSetting.objects.create(
        segment=segment,
        persona_name=data["persona_name"],
        voice=voice,
        opening_line=data["opening_line"],
        system_prompt=data["system_prompt"],
        behaviour=data.get("behaviour"),

        tone=data.get("tone", 72),
        pace=data.get("pace", 50),
        barge_in_threshold=data.get("barge_in_threshold", 65),

        max_turns=data.get("max_turns", 10),
        allow_customer_barge_in=data.get(
            "allow_customer_barge_in",
            True,
        ),
    )

    setting = (
        LLMSetting.objects
        .select_related("segment", "voice")
        .get(pk=setting.pk)
    )

    return Response({
        "success": True,
        "setting": _serialize_llm_setting(
            setting,
            with_metrics=False,
        ),
    }, status=201)

@api_view(['GET'])
def dealer_branches(request):
    """GET /branches/?dealer_id=1"""
    dealer_id = request.GET.get('dealer_id')
    if not dealer_id:
        return Response({"success": False, "error": "dealer_id is required"}, status=400)
    branches = Branch.objects.filter(dealer_id=dealer_id, flag='c', is_active=True).order_by('name')
    return Response({
        "success": True,
        "branches": [{"id": b.id, "name": b.name} for b in branches],
    })

@api_view(['GET'])
def dealers_list(request):
    """GET /dealers/ — for dealer selector dropdowns (e.g. global KB page)."""
    ds = Dealer.objects.filter(flag='c', is_active=True).order_by('name')
    return Response({
        "success": True,
        "dealers": [{"id": d.id, "name": d.name} for d in ds],
    })

@api_view(['GET', 'PATCH'])
def llm_setting_detail(request, segment_id):
    try:
        setting = LLMSetting.objects.select_related('segment', 'voice').get(segment__id=segment_id)
    except LLMSetting.DoesNotExist:
        return Response({"success": False, "error": "no LLM setting for this segment"}, status=404)

    if request.method == 'GET':
        return Response({"success": True, "setting": _serialize_llm_setting(setting, with_metrics=False)})

    data = request.data
    for field in ["persona_name", "opening_line", "system_prompt", "tone", "pace", "max_turns", "allow_customer_barge_in"]:
        if field in data:
            setattr(setting, field, data[field])
    if "voice_id" in data:
        try:
            setting.voice = TTSVoice.objects.get(id=data["voice_id"], is_active=True)
        except TTSVoice.DoesNotExist:
            return Response({"success": False, "error": "active TTS voice not found"}, status=404)
    setting.save()
    return Response({"success": True, "setting": _serialize_llm_setting(setting, with_metrics=False)})


def _serialize_customer_brief(customer) -> dict:
    if customer is None:
        return None
    return {
        "id": customer.id,
        "name": customer.name,
        "phone_number": customer.phone_number,
    }
 
 
def _serialize_vehicle_brief(vehicle) -> dict:
    if vehicle is None:
        return None
    return {
        "id": vehicle.id,
        "vehicle_name": vehicle.vehicle_name,
        "registration_no": vehicle.registration_no,
    }
 
 
def _serialize_segment_brief(segment) -> dict:
    if segment is None:
        return None
    data = {
        "id": segment.id,
        "name": segment.name,
    }

    module = getattr(segment, "module", None)
    if module is not None:
        data["module"] = module
    return data
 
 
def _serialize_llm_setting_brief(setting) -> dict:
    if setting is None:
        return None
    data = {
        "id": setting.id,
        "persona_name": setting.persona_name,
    }
    agent_name = getattr(setting, "agent_name", None)
    if agent_name:
        data["agent_name"] = agent_name
    module = getattr(setting, "module", None)
    if module is not None:
        data["module"] = module
    return data
 
 
def _serialize_recording(session) -> dict:
    """Serializes a CallSession row as a "recording" for the frontend."""
    return {
        "id": session.id,
        "session_id": str(session.session_id),
        "customer": _serialize_customer_brief(session.customer),
        "vehicle": _serialize_vehicle_brief(getattr(session, "vehicle", None)),
        "segment": _serialize_segment_brief(session.segment),
        "agent": _serialize_llm_setting_brief(session.agent),
        "status": session.status,
        "final_intent_code": session.final_intent_code,
        "direction": session.direction,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": session.duration_seconds,
        "transcript": session.transcript,
        "intent_history": session.intent_history,
        "call_summary": session.call_summary,
        "accuracy": session.accuracy,
        "recording_stereo": session.recording_stereo,
        "recording_mixed": session.recording_mixed,
    }
 
 
class _RecordingPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 500
 
 
@api_view(["GET"])
def recordings(request):
    """
    GET /api/recordings/?page_size=200&search=&module=&status=
    """
    qs = (
        CallSession.objects
        .select_related("customer", "vehicle", "segment", "agent")
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
 
    module = request.GET.get("module")
    if module:
        segment_fields = {f.name for f in Segment._meta.get_fields()}
        if "module" in segment_fields:
            qs = qs.filter(segment__module=module)
 
    paginator = _RecordingPagination()
    page = paginator.paginate_queryset(qs, request)
    data = [_serialize_recording(s) for s in page]
 
    return paginator.get_paginated_response(data)

@api_view(["GET"])
def recording_audio(request, session_id):
    """
    GET /api/recordings/<session_id>/audio/

    Streams the recording file straight off disk. recording_mixed /
    recording_stereo are stored as plain filesystem paths (not FileFields),
    so this reads them directly rather than going through MEDIA_URL.
    """
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
        logger.error(f"[BARGE-IN] settings lookup failed, using defaults: {e}")
        return BARGE_IN_DEFAULT_ENABLED, BARGE_IN_DEFAULT_RMS


def _resolve_dealer_branch_sync(phone_number, dealer_id=None):
    dealer = None
    if dealer_id:
        dealer = Dealer.objects.filter(pk=dealer_id, flag='c').first()
    if dealer is None:
        dealer = Dealer.objects.filter(flag='c').first()
    if dealer is None:
        logger.warning(f"[BRANCH] no active Dealer found (dealer_id={dealer_id})")
        return None, None

    branch = None
    if phone_number:
        customer = (
            Customer.objects.filter(dealer=dealer, phone_number=phone_number, flag='c')
            .select_related('default_branch__dealer')
            .first()
        )
        if customer:
            branch = customer.default_branch
            if branch is None:
                logger.warning(
                    f"[BRANCH] Customer found for phone={phone_number} but has "
                    f"no default_branch set"
                )
        else:
            logger.warning(
                f"[BRANCH] no existing Customer for dealer={dealer.pk}, "
                f"phone={phone_number} -- not creating one"
            )
    return dealer, branch


def _persist_recording_paths_sync(session_id, stereo_path, mixed_path, duration_seconds):
    from .models import CallSession
    updated = CallSession.objects.filter(session_id=session_id).update(
        recording_stereo=stereo_path,
        recording_mixed=mixed_path,
        duration_seconds=int(duration_seconds),
    )
    if not updated:
        logger.warning(
            f"[RECORD] No CallSession row found for session_id={session_id} "
            f"-- recording paths were not persisted"
        )
    return updated

# ============ QUICK CALL (test page) ============


NAME_RE = re.compile(r"^[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F .'-]*$")


def _json_error(exc):
    """Exception ko JSON me bhejo, HTML 500 page nahi."""
    payload = {'ok': False, 'error': f'{exc.__class__.__name__}: {exc}'}
    if getattr(settings, 'DEBUG', False):
        payload['trace'] = traceback.format_exc()
    return JsonResponse(payload, status=500)


def _norm_phone(raw):
    """Input se sirf digits lo, last 10 rakho. Valid ho to +91 laga ke return."""
    digits = re.sub(r'\D', '', raw or '')
    if len(digits) > 10:
        digits = digits[-10:]
    if len(digits) != 10 or digits[0] not in '6789':
        return None
    return '+91' + digits


def _clean_name(raw):
    """Sirf letters, space, dot, apostrophe, hyphen. Baaki reject."""
    name = re.sub(r'\s+', ' ', (raw or '').strip())
    if not name:
        return ''
    if len(name) > 60 or not NAME_RE.match(name):
        return None
    return name


def quick_call_page(request):
    return render(request, 'quick_call.html')


def quick_call_meta(request):
    try:
        data = []
        for d in Dealer.objects.all().order_by('name'):
            data.append({
                'id': d.id,
                'name': d.name,
                'branches': [
                    {'id': b.id, 'name': b.name}
                    for b in Branch.objects.filter(dealer=d).order_by('name')
                ],
            })
        return JsonResponse({'ok': True, 'dealers': data})
    except Exception as e:
        return _json_error(e)


@csrf_exempt
def quick_call_save(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    try:
        try:
            body = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

        phone = _norm_phone(body.get('phone_number'))
        if not phone:
            return JsonResponse(
                {'ok': False, 'error': '10-digit mobile number daalo (6/7/8/9 se shuru)'},
                status=400,
            )

        name = _clean_name(body.get('name'))
        if name is None:
            return JsonResponse(
                {'ok': False, 'error': 'Name me sirf letters, space, . - \' chal sakte hain'},
                status=400,
            )

        dealer_id = body.get('dealer_id')
        if not dealer_id:
            return JsonResponse({'ok': False, 'error': 'Dealer select karo'}, status=400)

        dealer = Dealer.objects.filter(id=dealer_id).first()
        if not dealer:
            return JsonResponse({'ok': False, 'error': 'Dealer nahi mila'}, status=404)

        branch = None
        if body.get('branch_id'):
            branch = Branch.objects.filter(id=body['branch_id'], dealer=dealer).first()
            if not branch:
                return JsonResponse({'ok': False, 'error': 'Branch is dealer ki nahi hai'}, status=400)

        # dealer + phone unique (flag='c') — dobara wahi number aaya to update
        customer = Customer.objects.filter(
            dealer=dealer, phone_number=phone, flag='c'
        ).first()

        if customer:
            created = False
            fields = []
            if name and customer.name != name:
                customer.name = name
                fields.append('name')
            if branch and customer.default_branch_id != branch.id:
                customer.default_branch = branch
                fields.append('default_branch')
            if fields:
                customer.save(update_fields=fields + ['updated_at'])
        else:
            created = True
            customer = Customer.objects.create(
                dealer=dealer, phone_number=phone, flag='c',
                name=name or '', default_branch=branch,
            )

        return JsonResponse({
            'ok': True,
            'created': created,
            'customer_id': customer.id,
            'name': customer.name,
            'phone_number': customer.phone_number,
            'dealer': dealer.name,
            'branch': branch.name if branch else None,
            'do_not_call': customer.do_not_call,
        })
    except Exception as e:
        return _json_error(e)


def quick_call_list(request):
    try:
        qs = (Customer.objects
              .filter(flag='c')
              .select_related('dealer', 'default_branch')
              .order_by('-id')[:20])
        return JsonResponse({'ok': True, 'customers': [{
            'id': c.id,
            'name': c.name,
            'phone_number': c.phone_number,
            'dealer': c.dealer.name,
            'branch': c.default_branch.name if c.default_branch else '-',
            'total_calls': c.total_calls,
            'do_not_call': c.do_not_call,
            'created_at': c.created_at.strftime('%d/%m/%Y %H:%M'),
        } for c in qs]})
    except Exception as e:
        return _json_error(e)

def _clean_reg_no(raw):
    """Registration number normalize: space/case hatao, dedup check isi pe hota hai."""
    if not raw:
        return ''
    return re.sub(r'\s+', '', raw.strip()).upper()


def quick_vehicle_page(request):
    return render(request, 'quick_vehicle.html')


def _serialize_customer_for_vehicle(c):
    """Customer + uski existing vehicles (dropdown me dikhane ke liye,
    taaki 'naya add karu ya purani update karu' user khud decide kare)."""
    return {
        'id': c.id,
        'name': c.name,
        'phone_number': c.phone_number,
        'dealer_id': c.dealer_id,
        'dealer': c.dealer.name if c.dealer else None,
        'default_branch_id': c.default_branch_id,
        'default_branch': c.default_branch.name if c.default_branch else None,
        'vehicles': [
            {
                'id': v.id,
                'vehicle_name': v.vehicle_name,
                'vehicle_model': v.vehicle_model,
                'registration_no': v.registration_no,
            }
            for v in c.vehicles.filter(flag='c', is_sold_off=False).order_by('-id')[:10]
        ],
    }


def quick_vehicle_customer_lookup(request):
    """
    GET /quick-vehicle/customer-lookup/?phone=98...  -> exact match
    GET /quick-vehicle/customer-lookup/?q=Rakesh       -> name/phone partial search (max 10)

    Sirf EXISTING customer dhoondta hai -- naya customer yahan se nahi
    banta (wo Quick Call page ka scope hai). Vehicle hamesha kisi
    existing Customer se linked honi chahiye.
    """
    try:
        phone = request.GET.get('phone')
        q = request.GET.get('q')

        if phone:
            norm = _norm_phone(phone)
            if not norm:
                return JsonResponse({'ok': False, 'error': '10-digit mobile number daalo'}, status=400)
            customer = (
                Customer.objects.filter(phone_number=norm, flag='c')
                .select_related('dealer', 'default_branch')
                .first()
            )
            if not customer:
                return JsonResponse({'ok': True, 'customer': None})
            return JsonResponse({'ok': True, 'customer': _serialize_customer_for_vehicle(customer)})

        if q:
            qs = (
                Customer.objects.filter(flag='c')
                .filter(Q(name__icontains=q) | Q(phone_number__icontains=q))
                .select_related('dealer', 'default_branch')
                .order_by('-id')[:10]
            )
            return JsonResponse({'ok': True, 'customers': [_serialize_customer_for_vehicle(c) for c in qs]})

        return JsonResponse({'ok': False, 'error': 'phone ya q chahiye'}, status=400)
    except Exception as e:
        return _json_error(e)


@csrf_exempt
def quick_vehicle_save(request):
    """
    POST /quick-vehicle/save/

    Body:
      customer_id             (required -- existing Customer.id)
      vehicle_id                (optional -- diya to UPDATE, warna CREATE)
      branch_id                  (optional -- last_service_branch; Customer.default_branch
                                   bhi isi se update hota hai, jaisa quick_call_save karta hai)
      vehicle_name, vehicle_model, registration_no
      last_service_type          ('none' | '1st_free' | '2nd_free' | '3rd_free' | 'paid')
      last_service_date          (YYYY-MM-DD, optional)
      next_service_due_date      🔥 CONTEXT-CRITICAL -- get_customer_context()'s due_date
                                   aur module ('service_reminder' vs 'general_query') isi
                                   se aate hain, aur Segment(trigger_type='service_due')
                                   scheduler bhi isi field pe query maarta hai.
      insurance_expiry_date      🔥 CONTEXT-CRITICAL for insurance_due segment
      amc_expiry_date            🔥 CONTEXT-CRITICAL for amc_due segment
      on_reminder                 (default true -- False = is gaadi ke liye reminder band)

    Agar next_service_due_date khali chhoda gaya HO lekin last_service_type +
    last_service_date diye hain, to ServiceSchedule rule se (scheduler jaisa
    hi logic) auto-calculate karne ki koshish karta hai. Diya hi gaya ho to
    seedha wahi save hota hai -- koi override nahi.
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    try:
        try:
            body = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

        customer_id = body.get('customer_id')
        if not customer_id:
            return JsonResponse({'ok': False, 'error': 'customer_id chahiye'}, status=400)

        customer = Customer.objects.filter(id=customer_id, flag='c').select_related('dealer').first()
        if not customer:
            return JsonResponse({'ok': False, 'error': 'Customer nahi mila'}, status=404)

        branch = None
        if body.get('branch_id'):
            branch = Branch.objects.filter(id=body['branch_id'], dealer=customer.dealer).first()
            if not branch:
                return JsonResponse({'ok': False, 'error': 'Branch is dealer ki nahi hai'}, status=400)

        reg_no = _clean_reg_no(body.get('registration_no'))
        vehicle_name = (body.get('vehicle_name') or '').strip()
        vehicle_model = (body.get('vehicle_model') or '').strip()

        if not vehicle_name and not reg_no:
            return JsonResponse(
                {'ok': False, 'error': 'Vehicle name ya registration number me se ek chahiye'},
                status=400,
            )

        last_service_type = body.get('last_service_type') or 'none'
        valid_types = {c[0] for c in SERVICE_TYPE_CHOICES}
        if last_service_type not in valid_types:
            return JsonResponse(
                {'ok': False, 'error': f'invalid last_service_type: {last_service_type}'}, status=400,
            )

        def _pdate(key):
            raw = body.get(key)
            if not raw:
                return None
            try:
                return datetime.strptime(raw, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError(f'{key}: YYYY-MM-DD format me do')

        try:
            last_service_date = _pdate('last_service_date')
            next_service_due_date = _pdate('next_service_due_date')
            insurance_expiry_date = _pdate('insurance_expiry_date')
            amc_expiry_date = _pdate('amc_expiry_date')
        except ValueError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

        # 🔥 next_service_due_date khali hai par last_service_type/date diye
        # hain -- ServiceSchedule rule se auto-calculate karo (scheduler ka
        # wahi logic): specific vehicle_model rule pehle, blank ('' = sab
        # models ka default) fallback.
        next_service_type = ''
        if next_service_due_date is None and last_service_date is not None:
            rule = (
                ServiceSchedule.objects.filter(
                    dealer=customer.dealer, from_service=last_service_type, flag='c',
                )
                .filter(Q(vehicle_model=vehicle_model) | Q(vehicle_model=''))
                .order_by('-vehicle_model')  # non-blank (specific) > '' lexicographically
                .first()
            )
            if rule:
                next_service_due_date = last_service_date + timedelta(days=rule.days_after)
                next_service_type = rule.to_service

        vehicle_id = body.get('vehicle_id')
        if vehicle_id:
            vehicle = Vehicle.objects.filter(id=vehicle_id, customer=customer, flag='c').first()
            if not vehicle:
                return JsonResponse({'ok': False, 'error': 'Vehicle nahi mili is customer ki'}, status=404)
            created = False
        else:
            # Same dealer + same reg_no already exists to duplicate row mat banao.
            vehicle = None
            if reg_no:
                vehicle = Vehicle.objects.filter(
                    dealer=customer.dealer, registration_no=reg_no, flag='c',
                ).first()
            created = vehicle is None
            if vehicle is None:
                vehicle = Vehicle(dealer=customer.dealer, customer=customer)

        vehicle.customer = customer
        vehicle.vehicle_name = vehicle_name
        vehicle.vehicle_model = vehicle_model
        vehicle.registration_no = reg_no
        vehicle.last_service_type = last_service_type
        vehicle.last_service_date = last_service_date
        if next_service_type:
            vehicle.next_service_type = next_service_type
        vehicle.next_service_due_date = next_service_due_date
        vehicle.insurance_expiry_date = insurance_expiry_date
        vehicle.amc_expiry_date = amc_expiry_date
        vehicle.on_reminder = bool(body.get('on_reminder', True))
        if branch:
            vehicle.last_service_branch = branch
            if not vehicle.purchased_branch_id:
                vehicle.purchased_branch = branch

        vehicle.save()

        # 🔥 Client rule (same as quick_call_save): jis branch pe service
        # karvai wahi customer ka naya default_branch.
        if branch and customer.default_branch_id != branch.id:
            customer.default_branch = branch
            customer.save(update_fields=['default_branch', 'updated_at'])

        return JsonResponse({
            'ok': True,
            'created': created,
            'vehicle_id': vehicle.id,
            'vehicle_name': vehicle.vehicle_name,
            'registration_no': vehicle.registration_no,
            'next_service_due_date': (
                vehicle.next_service_due_date.isoformat() if vehicle.next_service_due_date else None
            ),
            'module_hint': 'service_reminder' if vehicle.next_service_due_date else 'general_query',
        })
    except Exception as e:
        return _json_error(e)


def quick_vehicle_list(request):
    try:
        qs = (
            Vehicle.objects.filter(flag='c')
            .select_related('customer', 'last_service_branch')
            .order_by('-id')[:20]
        )
        return JsonResponse({'ok': True, 'vehicles': [{
            'id': v.id,
            'vehicle_name': v.vehicle_name,
            'vehicle_model': v.vehicle_model,
            'registration_no': v.registration_no,
            'customer': v.customer.name,
            'phone_number': v.customer.phone_number,
            'last_service_type': v.last_service_type,
            'last_service_date': v.last_service_date.strftime('%d/%m/%Y') if v.last_service_date else '-',
            'next_service_due_date': (
                v.next_service_due_date.strftime('%d/%m/%Y') if v.next_service_due_date else '-'
            ),
            'insurance_expiry_date': (
                v.insurance_expiry_date.strftime('%d/%m/%Y') if v.insurance_expiry_date else '-'
            ),
            'amc_expiry_date': v.amc_expiry_date.strftime('%d/%m/%Y') if v.amc_expiry_date else '-',
            'on_reminder': v.on_reminder,
            'branch': v.last_service_branch.name if v.last_service_branch else '-',
        } for v in qs]})
    except Exception as e:
        return _json_error(e)