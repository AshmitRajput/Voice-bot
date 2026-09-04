from nltk import chunk
import json
import base64
import asyncio
import time
import audioop
import uuid
import logging
import threading
import datetime
from decimal import Decimal, ROUND_HALF_UP      # 🔥 NEW: cost math
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import numpy as np          # ← yeh line add karo
from .services.cloud_llm_service import chat_turn_stream
from .services.tts_service import get_tts_service
import wave
import io
from pydub import AudioSegment  # pip install pydub
import os
from django.conf import settings
from .services.filler_service import (
    classify_intent, filler_for_intent, should_run_rag, get_intent_model,
)
from .services.rag_service import get_rag_service
from .services.stt_service import STTSession
# Redis state -- as-is, untouched
from .services.conversation_history import (
    init_state, set_speech_state, set_final_transcript, set_generating, clear_state,
    save_conversation,
)
# 🔥 SAB DB kaam ab views.py se
# 🔥 NEW: save_turn_scores -- persists per-turn accuracy/filler_accuracy/
# llm_pricing onto an already-created ConversationTurn row by id.
# 🔥 NEW: log_service_error -- provider failures ka DB row (ServiceErrorLog).
from .views import (
    get_or_create_call_session, get_customer_context, get_customer_context_by_phone,
    get_random_customer_context, save_turn, end_call_session, finalize_call_summary,
    get_history_for_llm, save_turn_scores, log_service_error,
)
from .services.cloud_llm_service import chat_turn_stream, build_timing_record, score_and_price_turn, get_last_turn_usage
from .services.filler_audio_cache import load_cached_pcm
from urllib.parse import parse_qs
from django.utils import timezone

from .views import (
    extract_slot_request, extract_slot_continuation, mentions_confirmation,
    get_available_slots, format_slots_for_reference, book_slot_for_session,
)

from .views_admin import _get_barge_in_settings_sync, _resolve_dealer_branch_sync, _persist_recording_paths_sync
from .tools.callback_tools import schedule_callback
from .tools.tool_registry import should_end_call, register_end_call_handler, unregister_end_call_handler, set_call_context, clear_call_context
# 🔥 BARGE-IN SETTINGS: admin-configurable toggle + loudness threshold,
# read once per call in connect() (see _get_barge_in_settings_sync below).
from .models import LLMSetting, Dealer, Branch, Customer
import re
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

USE_DETERMINISTIC_BOOKING = False

ENABLE_INTENT_AND_FILLER = True

DEBUG_VAD_RMS = False

logger = logging.getLogger('voice_bot')


# ═══════════════════════════════════════════════════════════════
# 🔥 NEW: COST CALCULATION
# ---------------------------------------------------------------
# Per-turn STT/TTS aur per-call dialer ka paisa. LLM ka cost YAHAN
# NAHI hai -- wo already score_and_price_turn() (per turn) aur
# generate_call_summary() (per call) se aata hai, unhe chhua nahi.
#
# Rates rupees me. ConversationTurn.stt_pricing/tts_pricing aur
# CallSession.dialer_pricing sab DecimalField(decimal_places=6) hain,
# isliye Decimal use kar rahe hain -- float me 6 decimal ka rounding
# turn-dar-turn jamaa hoke galat total banata hai.
#
# views.recalc_call_cost() cost_dialer() ko yahin se deferred-import
# karta hai (circular import se bachne ke liye).
# ═══════════════════════════════════════════════════════════════
# --- STT: Gnani (WebSocket) ₹27 per hour ---
STT_PER_HOUR = Decimal('27')

# --- TTS: Murf Falcon $0.01 per 1000 chars ---
# USD me quote hota hai, isliye FX rate alag rakha -- rupya hile to
# sirf ye ek line badalni hai.
USD_TO_INR       = Decimal('88')
TTS_USD_PER_1K   = Decimal('0.01')
TTS_PER_1K_CHARS = TTS_USD_PER_1K * USD_TO_INR      # ≈ ₹0.88

_COST_Q = Decimal('0.000001')       # 6 decimal places -- field ke barabar

def _quantize_cost(value):
    return Decimal(value).quantize(_COST_Q, rounding=ROUND_HALF_UP)


def cost_stt_from_bytes(audio_bytes_len, sample_rate=16000):
    """PCM16 mono bytes → STT cost. Gnani per-hour bill karta hai."""
    if not audio_bytes_len:
        return Decimal('0')
    seconds = Decimal(int(audio_bytes_len)) / (sample_rate * 2)
    return _quantize_cost(seconds / 3600 * STT_PER_HOUR)


def cost_tts(chars):
    """Murf ko bheje gaye characters ka cost. Cached filler ke liye 0
    aata hai -- uske liye TTS engine call hi nahi hota."""
    if not chars:
        return Decimal('0')
    return _quantize_cost(Decimal(int(chars)) / 1000 * TTS_PER_1K_CHARS)




_LEAK_PATTERNS = re.compile(
    r'(TOOL\s*CALL\s*:|function_call|book_slot\s*\(|check_availability\s*\(|\{"intent"|\{\s*"response_text)',
    re.IGNORECASE,
)

def _now_ist():                        
    return timezone.now().astimezone(IST)
    
# Google STT lazy init
google_stt_client = None

def get_stt_client():
    global google_stt_client
    if google_stt_client is None:
        try:
            from google.cloud import speech
            google_stt_client = speech.SpeechClient()
            print("✅ [STT] Google Speech-to-Text initialized")
        except Exception as e:
            print(f"❌ [STT] init failed: {e}")
    return google_stt_client


# ⚠️ PLACEHOLDER: book_slot_for_session()/format_slots_for_reference() live in
# views_admin.py, but _format_date_hi/_format_time_hi (used to phrase the
# spoken confirmation in process_utterance's booking block) weren't defined
# anywhere in either version of this file. Minimal best-effort versions are
# provided here so the booking flow doesn't crash with NameError -- if a
# real implementation already exists in views_admin.py, delete these and
# import from there instead so formatting stays consistent everywhere.
_HI_MONTHS = {
    1: "जनवरी", 2: "फ़रवरी", 3: "मार्च", 4: "अप्रैल", 5: "मई", 6: "जून",
    7: "जुलाई", 8: "अगस्त", 9: "सितंबर", 10: "अक्टूबर", 11: "नवंबर", 12: "दिसंबर",
}

def _format_date_hi(iso_date: str) -> str:
    """'2026-08-20' -> '20 अगस्त'."""
    d = datetime.date.fromisoformat(iso_date)
    return f"{d.day} {_HI_MONTHS.get(d.month, d.strftime('%B'))}"

def _format_time_hi(time_str: str) -> str:
    """'14:30' -> '2:30 बजे'."""
    try:
        t = datetime.datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return time_str
    hour12 = t.hour % 12 or 12
    return f"{hour12}:{t.strftime('%M')} बजे"

_background_tasks = set()

def _fire_and_forget(coro, label=""):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t):
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(f"[BG] background task failed ({label}): {exc}")

    task.add_done_callback(_on_done)
    return task

# 🔥 BARGE-IN SETTINGS (DB-backed)
# ------------------------------------------------------------------
# Reads LLMSetting.allow_customer_barge_in / .barge_in_threshold and
# converts the 0-100 admin/customer slider into an actual RMS value the
# hot-path barge-in code can compare against directly.
#
# NOTE: LLMSetting is per-Segment (OneToOneField(Segment)), but neither
# CallSession nor Customer currently carries a segment FK -- so for now
# this loads a single settings row (segment_name=None -> .first()).
# Once a call can be resolved to a specific segment (e.g. via
# customer.segment / cloud_context["module"]), pass that name in here
# and it'll resolve the per-segment row instead -- nothing else in
# consumers.py needs to change.
#
# Sync (plain Django ORM) function -- always call this wrapped in
# database_sync_to_async(...) from async code, never call it directly.
BARGE_IN_THRESHOLD_MIN_RMS = 700    # slider=0   -> most sensitive
BARGE_IN_THRESHOLD_MAX_RMS = 2200   # slider=100 -> least sensitive
BARGE_IN_DEFAULT_ENABLED = True
BARGE_IN_DEFAULT_RMS = 900          # used only if no LLMSetting row exists yet


def _rag_ask_sync(dealer, module, branch, question, top_k=3):
    """Sync wrapper so consumers.py can call it via asyncio.to_thread, same
    pattern as _get_barge_in_settings_sync. Fails soft (empty result) if no
    dealer was resolved for this call, instead of raising into the hot path."""
    if dealer is None:
        return {"success": False, "error": "no dealer resolved", "contexts": [], "sources": [], "best_distance": None}
    return get_rag_service().ask_question(
        dealer=dealer, module=module, question=question, branch=branch, top_k=top_k
    )

class VoiceChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket: Browser → Google STT → Cloud LLM (Gemini + RAG, Aarohi persona) → Murf TTS Streaming
    """

    # 🔥 NEW: bot TTS ka native sample rate — recording sync ke liye use hoga
    BOT_AUDIO_SAMPLE_RATE = 24000  # 🔥 FIX: must match MurfTTS.synthesize_stream's
                                    # sample_rate=24000 in tts_service.py -- this was
                                    # 22050 while Murf actually streams 24000Hz PCM,
                                    # so bot audio was being recorded/estimated at the
                                    # wrong rate (and, if the frontend player was also
                                    # reading this value, played back pitched/garbled).

    PLAYBACK_LEAD_MS = 200  # tuned for the browser/WS test client -- if you're
                             # testing against a DIFFERENT client (real dialer/SIP
                             # bridge), re-measure this against that client's real
                             # playback buffering before trusting it.

    # 🔥 NEW: REAL-TIME PACED SEND -- ported from the Plivo dialer's
    # PLAYOUT_LEAD_S. Caps how far "ahead" of real playback time bot audio
    # is allowed to sit once it's been handed to safe_send(). See
    # _send_bot_pcm() for why this is what actually makes barge-in cut off
    # audio promptly instead of merely stopping the SERVER from producing
    # more (while a client-side buffer full of already-sent audio keeps
    # playing regardless). Deliberately a separate constant from
    # PLAYBACK_LEAD_MS above, which only affects how much tail is trimmed
    # from the SAVED recording, not what's sent over the wire.
    PACED_SEND_LEAD_S = 0.6

    # ═══════════════════════════════════════════════════════════
    # 🔥 NEW: ERROR LOGGING HELPER
    # ═══════════════════════════════════════════════════════════
    def _log_error(self, provider, stage, exc, severity='error', **context):
        """Provider failure ka ServiceErrorLog row -- fire-and-forget.

        Live call kabhi isse block/fail nahi hoti: coroutine background
        me jaata hai aur log_service_error() khud apne andar try/except
        me lipta hai.
        """
        try:
            _fire_and_forget(
                database_sync_to_async(log_service_error)(
                    session_id=getattr(self, 'session_id', None),
                    provider=provider,
                    stage=stage,
                    severity=severity,
                    error_type=type(exc).__name__ if isinstance(exc, BaseException) else 'Error',
                    error_message=str(exc),
                    context=context or {},
                ),
                label=f"errlog:{provider}/{stage}",
            )
        except Exception as e:
            # Logging ki koshish khud fail -- console tak hi rakho.
            logger.warning(f"[ERRLOG] could not schedule error log ({provider}/{stage}): {e}")

    def _on_end_call_signal(self, payload):
        """Fires synchronously on the event loop the instant end_call's tool
        impl runs, mid-LLM-stream -- well before this turn's closing line has
        even started TTS. Setting this flag here (not in a poll after the
        turn finishes) is what lets barge-in checks below refuse to interrupt
        this turn."""
        self._call_ending = True
        print(f"🔒 [END-CALL] signalled early (reason={payload.get('reason')}) -- "
            f"this turn is now barge-in-immune")

    async def connect(self):
        await self.accept()
        self._closed = False
        self._active_tasks = set()
        self._barge_in_fired = False   # guards against double-firing before state settles
        self._bot_speaking_clear_task = None  # fallback timer for bot_speaking (see _arm_bot_speaking_fallback)
        self._playout_end = 0.0  # 🔥 NEW: real-time paced-send clock, see _send_bot_pcm
        self._noise_floor = 300.0
        self._adaptive_threshold = self.VOICE_RMS_THRESHOLD
        self._gnani_speech_end = False  # 🔥 LATENCY: set by STT backend's own speech-end signal (see _on_speech_end_signal)
        self._turn_start_time = None   # 🔥 FIX 1: wall-clock time this turn started speaking/generating,
                                        # used as a min-turn-age guard so a turn can't be barge-in-killed
                                        # within its own first instant
        self._barge_in_frame_count = 0       # 🔥 FIX 3: consecutive above-threshold ("loud") chunks
        self._barge_in_voiced_frame_count = 0  # 🔥 NEW: of those loud chunks, how many also looked voice-pitched
        self._barge_in_cooldown_until = 0.0  # 🔥 FIX 4: refractory period right after a barge-in fires

        # 🔥 BARGE-IN SETTINGS: filled in a few lines below, once the DB
        # lookup completes -- defaulted here so nothing on the hot path
        # ever sees an unset attribute if audio somehow arrives before
        # that lookup finishes.
        self._barge_in_enabled = BARGE_IN_DEFAULT_ENABLED
        self._barge_in_rms_threshold = BARGE_IN_DEFAULT_RMS

        # 🔥 BARGE-IN RECORDING ACCURACY -- continuous, CALL-scoped (not
        # per-turn) play-cursor simulator. Tracks how far into
        # recording["bot_audio"] the customer has actually (virtually)
        # heard, by playing at the true sample-rate pace and freezing the
        # instant it catches up to whatever has actually been written.

        self._bot_play_position = 0
        self._bot_play_anchor_time = None
        self._bot_play_anchor_offset = 0

        self.session_id = str(uuid.uuid4())

        self._call_ending = False
        register_end_call_handler(self.session_id, asyncio.get_event_loop(), self._on_end_call_signal)

        # 🔥 Identify the customer for this call from the websocket URL,
        # e.g. ws://.../ws/voice/?phone=9999999999 -- the frontend passes
        # this when it opens the connection. No param = generic/anonymous
        # call, same as before (falls back to "Customer"/"Unknown").
        query_params = parse_qs((self.scope.get("query_string") or b"").decode())
        self.phone_number = (query_params.get("phone") or [None])[0]
        self.dealer_id_param = (query_params.get("dealer_id") or [None])[0]

        self.session = {
            "history": [],
            "is_processing": False,
            "bot_speaking": False,
            "has_conversation": False,
            "booking_confirmed": None,
        }
        self._write_cursor = {"user": 0, "bot": 0}   # 🔥 NEW: har source ka apna sequential cursor

        # 🔥 NEW: ab ek hi single "mixed_audio" buffer hai — user aur bot dono ki
        # audio isi mein unki real elapsed-time position par likhi jaati hai.
        # Isse alag-alag record karke baad mein pad/merge karne ki zaroorat khatam,
        # aur overlap/drift ka issue bhi khatam.
        self.recording = {
            "active": True,
            "user_audio": bytearray(),   # 🔥 alag channel — clipping/distortion khatam
            "bot_audio": bytearray(),
            "start_time": time.time(),
            "transcript": [],
        }

        # single lock — user aur bot dono writes isi lock ke andar honge (thread-safe positional write)
        self._audio_lock = asyncio.Lock()
        self.RECORD_SAMPLE_RATE = 16000   # final recording sample rate
        self._stt_session = None  # placeholder -- replaced a few lines below with a persistent,
                                   # call-scoped STTSession (Sarvam primary / Google fallback)
        self._barge_in_buffer = bytearray()
        self._barge_in_voice_start = None
        self._tts_send_lock = asyncio.Lock()
        self._current_process_task = None
        self._turn_items = []
        self._turn_items_lock = asyncio.Lock()
        self._turn_items_done = False

        # 🔥 NEW: single ORDERED writer for bot-audio recording writes.
        self._bot_write_queue = asyncio.Queue()
        self._bot_write_task = asyncio.create_task(self._bot_write_worker())

        # 🔥 NEW: running per-call LLM token totals, built up turn-by-turn
        # from score_and_price_turn()'s return value (each turn's MAIN
        # generation usage, not the scoring call's own usage). Passed into
        # finalize_call_summary() at call-end so generate_call_summary()
        # can compute the whole-call llm_pricing.
        self._session_prompt_tokens = 0
        self._session_output_tokens = 0

        # 🔥 NEW: TTS COST TRACKING (per turn)
        # _turn_tts_chars: is turn me Murf ko bheje gaye characters. Har
        #   turn ki shuruaat me 0 hota hai, _schedule_tts() badhata hai.
        #   Cached filler ise nahi badhata -- uska koi paisa nahi lagta.
        # _greeting_tts_chars: greeting send_greeting() me bolti hai, par
        #   uska koi ConversationTurn row nahi banta (CallSession tab tak
        #   exist hi nahi karta). Isliye uske chars yahan park hote hain
        #   aur PEHLE bot turn ke saath DB me chale jaate hain -- warna
        #   ~100 char ka TTS har call me gum ho jaata.
        self._turn_tts_chars = 0
        self._greeting_tts_chars = 0

        self._score_tasks = []
        self._intent_history = []
        self._filler_history = []
        self._customer_text_history = []
        self._bot_resample_state = None

        # ConversationState init -- fire-and-forget, doesn't block connect()
        asyncio.create_task(database_sync_to_async(init_state)(self.session_id))

        if self.phone_number:
            context = await database_sync_to_async(get_customer_context_by_phone)(self.phone_number)
            print(f"Phone Context: {context}")
            if context is None:
                logger.warning(
                    f"[WS] no Customer found for phone={self.phone_number} -- "
                    f"falling back to a random seeded customer's context for this demo call."
                )
                context = await database_sync_to_async(get_random_customer_context)()
        else:
            # No phone at all in the URL -- genuinely a "demo/random" call.
            context = await database_sync_to_async(get_random_customer_context)()

        self.customer_id = context.get("customer_id")
        effective_phone = context.get("phone_number") or self.phone_number
        self._effective_phone = effective_phone

        self._call_session_created = False
        self._call_session_lock = asyncio.Lock()
        self._call_session_task = None

        self.dealer, self.branch = await database_sync_to_async(_resolve_dealer_branch_sync)(
            effective_phone, self.dealer_id_param
        )
        if self.dealer is None:
            logger.error(f"[RAG] no dealer resolved for session {self.session_id} — RAG will be skipped this call")
        
        self.session["cloud_context"] = {
            "customer_name": context["customer_name"],
            "vehicle_model": context["vehicle_model"],
            "due_date": context["due_date"],
            "module": context["module"],
            "branch": getattr(self.branch, "name", None) or context.get("branch") or "Unknown",
            "current_datetime_ist": _now_ist().strftime("%Y-%m-%d %H:%M"),
        }

        set_call_context(
            self.session_id,
            phone_number=effective_phone,
            customer_name=self.session["cloud_context"].get("customer_name"),
        )

        allow_barge_in, barge_in_rms = await database_sync_to_async(_get_barge_in_settings_sync)()
        self._barge_in_enabled = allow_barge_in
        self._barge_in_rms_threshold = barge_in_rms
        print(f"🔧 [BARGE-IN] enabled={allow_barge_in}, rms_threshold={barge_in_rms}")

        print(f"🔌 [WS] Client connected, session_id={self.session_id}, "
              f"phone={self.phone_number}, context={self.session['cloud_context']}")

        # 🔥 PERSISTENT STT: ONE Sarvam WebSocket connection for the whole
        # call, not one per utterance -- opened once here and reused every
        # turn via begin_turn()/feed()/end_turn() (see stt_service.py).
        # Fired as a background task BEFORE the greeting so its handshake
        # latency overlaps with greeting synthesis/playback instead of
        # adding to the first turn's latency. If Sarvam can't be reached
        # in time, every turn transparently falls back to per-utterance
        # Google until it recovers -- no code here needs to know which.
        self._stt_session = STTSession(sample_rate=16000)
        # 🔥 LATENCY: STT backend's own server-side VAD tells us speech
        # ended well before our local SILENCE_HANGOVER timer would --
        # dispatch on that signal instead of always waiting out the full
        # local window (see _on_speech_end_signal / handle_audio).
        self._stt_session.on_speech_end = self._on_speech_end_signal
        self._stt_connect_task = self._track(self._stt_session.connect())

        # Intent classifier + RAG service are warmed up once at process
        # startup now (see asgi.py) -- no per-connection warmup needed here.
        await self.send_greeting()

    async def disconnect(self, close_code):
        print(f"🔌 [WS] Disconnected: {close_code}")
        unregister_end_call_handler(self.session_id)
        clear_call_context(self.session_id)
        # 🔥 NEW: Save recording only if conversation happened
        if self.session.get("has_conversation") and self.recording.get("active"):
            try:
                if getattr(self, "_call_session_task", None):
                    await self._call_session_task  # make sure the row exists first
                await self.save_conversation_recording()
            except Exception as e:
                print(f"❌ [RECORD] Failed to save: {e}")
                # 🔥 NEW: recording save failure ka log
                self._log_error("recording", "save_conversation_recording", e,
                                severity="error", close_code=close_code)
        self._closed = True
        pending = [t for t in self._active_tasks if not t.done()]
        print(f"🔌 [WS] Disconnected: {close_code}, cancelling {len(pending)} pending task(s)")
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # 🔥 NEW: shut down the ordered bot-audio write worker last, after
        # any in-flight turn has been cancelled/settled above -- and only
        # here, at call teardown (never mid-call, unlike the old per-chunk
        # tasks which lived in self._active_tasks and got swept up by
        # every _cancel_current_turn()).
        if getattr(self, "_bot_write_task", None) is not None:
            self._bot_write_task.cancel()
            try:
                await self._bot_write_task
            except (asyncio.CancelledError, Exception):
                pass

        if getattr(self, "_stt_session", None) is not None:
            try:
                await self._stt_session.close()
            except Exception as e:
                print(f"⚠️ [WS] STT session close error: {e}")
                # 🔥 NEW
                self._log_error("stt", "session_close", e, severity="warning")
        _fire_and_forget(database_sync_to_async(clear_state)(self.session_id), label="clear_state")
        status = "completed" if self.session.get("has_conversation") else "dropped"
        # 🔥 NOTE: end_call_session() ab andar hi recalc_call_cost() call
        # karta hai -- wahin duration_seconds set hota hai, isliye dialer
        # ka pulse cost pehli baar wahan compute hota hai.
        _fire_and_forget(
            database_sync_to_async(end_call_session)(self.session_id, status),
            label="end_call_session",
        )
        if self.session.get("has_conversation"):
            _fire_and_forget(
                database_sync_to_async(finalize_call_summary)(
                    self.session_id, self.session.get("cloud_context"),
                    total_prompt_tokens=self._session_prompt_tokens,
                    total_output_tokens=self._session_output_tokens,
                    intent_history=self._intent_history,
                    filler_history=self._filler_history,
                    customer_text_history=self._customer_text_history,   # 🔥 NEW
                ),
                label="finalize_call_summary",
            )

    def _track(self, coro):
        """Wrap asyncio.create_task so the task is cancellable on disconnect/interrupt."""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _ensure_call_session(self):
        """Lazily create the CallSession row -- only once this connection has
        proven itself real by exchanging at least one message with the
        client, instead of unconditionally on every WebSocket connect().
        """
        if self._call_session_created:
            return
        async with self._call_session_lock:
            if self._call_session_created:
                return
            self._call_session_created = True
            self._call_session_task = asyncio.create_task(
                database_sync_to_async(get_or_create_call_session)(
                    self.session_id, phone_number=self._effective_phone, branch=self.branch
                )
            )

    def _on_speech_end_signal(self):
        """🔥 LATENCY: fires when the STT backend's own server-side VAD
        detects end-of-speech, typically well before our own local
        SILENCE_HANGOVER timer would. Sync callback (invoked from the STT
        recv-loop thread/coroutine) -- deliberately just sets a flag here;
        the actual turn dispatch happens on the next audio chunk in
        handle_audio(), where the full VAD state (buffer, speech_started,
        stt_session) is already in hand. If this signal never arrives
        (backend down, or the per-utterance Google fallback path, which
        has no server-side VAD of its own), handle_audio() falls back
        unchanged to the local SILENCE_HANGOVER wait.
        """
        if self._speech_started:
            self._gnani_speech_end = True

    def _reset_barge_in_state(self):
        """Clear any partially-accumulated barge-in buffer. Must be called
        whenever bot_speaking flips to False for any reason (natural
        playback_end, explicit interrupt, or a fired barge-in) -- otherwise
        a noise blip that didn't sustain long enough to trigger gets left
        sitting in the buffer and silently prepends itself to the START of
        the NEXT turn's barge-in audio the next time the bot speaks."""
        self._barge_in_buffer = bytearray()
        self._barge_in_voice_start = None
        self._barge_in_frame_count = 0
        self._barge_in_voiced_frame_count = 0  # 🔥 NEW: pitch-confirmed subset of the loud frames
        self._barge_in_last_loud_time = None

        # 🔥 NEW: re-prime VAD calibration every time bot_speaking clears.
        # _noise_floor/_adaptive_threshold were previously left completely
        # frozen for the whole bot_speaking/is_processing window (often
        # 5-9+ seconds per turn, per the logs) -- if acoustic conditions
        # shifted during that window (line noise, gain change, echo from
        # the bot's own TTS bleeding into the mic), the stale threshold
        # could sit too high for genuine speech right afterward and just
        # never fire speech_start, with no error anywhere. Resetting to a
        # known-good default here means at worst a turn or two of slightly
        # conservative calibration while it re-adapts, instead of a VAD
        # that's silently stuck too insensitive for the rest of the call.
        self._noise_floor = 300.0
        self._adaptive_threshold = self.VOICE_RMS_THRESHOLD

    def _reset_playout_state(self):
        """🔥 NEW: reset the real-time paced-send clock (see _send_bot_pcm).
        Not strictly load-bearing -- _send_bot_pcm() re-anchors to "now"
        on its own the next time it's called after a gap -- but calling
        this alongside _reset_barge_in_state() keeps the two state resets
        symmetric and makes a fresh turn's pacing start from a clean
        baseline instead of whatever was left over from the last one."""
        self._playout_end = 0.0

    def _cancel_bot_speaking_fallback(self):
        """Cancel the pending fallback clear-timer, if any -- called when a
        real playback_end/interrupt arrives so the fallback doesn't fire
        redundantly later."""
        task = getattr(self, "_bot_speaking_clear_task", None)
        if task and not task.done():
            task.cancel()

    async def _clear_bot_speaking_after_delay(self, delay, tag):
        """Fallback safety-net ONLY. Waits for the ESTIMATED audio playback
        duration (plus a buffer) before clearing bot_speaking, instead of
        clearing it the instant the server finishes SENDING bytes.

        The CORRECT signal is the client's own "playback_end" message (see
        handle_json), sent when it's actually done playing. This method is
        only the fallback for when that message is lost/never arrives
        (client crash, dropped message, etc.) -- without SOME fallback,
        bot_speaking could get stuck True forever and silently break VAD
        for the rest of the call.

        🔥 NEW: every time this fallback actually has to fire (i.e.
        playback_end never showed up), that's a signal the estimate/lead
        constants (PACED_SEND_LEAD_S, PLAYBACK_LEAD_MS -- both tuned
        against a specific test client) may not match the real client's
        playback latency, and downstream state (VAD dispatch gating, the
        recording's bot-audio positioning) is now running on a guess
        instead of ground truth. Logged loudly so it's easy to see how
        often this is actually happening in a given call.
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # a real playback_end arrived first -- nothing to do
        if self.session.get("bot_speaking"):
            print(f"⚠️ [BOT] playback_end never arrived for {tag} "
                  f"(waited {delay:.2f}s) -- clearing bot_speaking via fallback timer. "
                  f"If this keeps happening, the client isn't sending playback_end "
                  f"reliably and PACED_SEND_LEAD_S/PLAYBACK_LEAD_MS need re-tuning "
                  f"for the real client's playback latency.")
            logger.warning(
                "[BOT] playback_end never arrived for %s (waited %.2fs) -- "
                "using fallback timer", tag, delay,
            )
            self.session["bot_speaking"] = False
            self._reset_barge_in_state()

    async def _send_bot_pcm(self, chunk: bytes):
        now = time.time()
        if self._playout_end < now:
            self._playout_end = now

        ahead = self._playout_end - now
        if ahead > self.PACED_SEND_LEAD_S:
            try:
                await asyncio.sleep(ahead - self.PACED_SEND_LEAD_S)
            except asyncio.CancelledError:
                raise

        if self._closed:
            return

        await self.safe_send(bytes_data=chunk)

        # 🔥 FIX: was `self._track(self._write_positional_delayed(chunk, ...))`
        # -- an independent asyncio.sleep() task per chunk, whose completion
        # order isn't guaranteed under event-loop load. That reordering fed
        # audioop.ratecv's stateful resampler out of temporal order and
        # misplaced chunks relative to the write cursor, which is what was
        # producing overlapping/garbled bot_audio in saved recordings.
        # Now: push (chunk, target_write_time) onto a single ordered queue;
        # one dedicated worker (_bot_write_worker) always writes in the
        # exact order chunks were sent. See that method + _drain_bot_write_queue.
        write_at = time.time() + self.PACED_SEND_LEAD_S
        await self._bot_write_queue.put((chunk, write_at))

        self._playout_end += len(chunk) / (self.BOT_AUDIO_SAMPLE_RATE * 2)

    async def _bot_write_worker(self):
        """🔥 NEW: single ordered consumer for bot-audio recording writes.

        Replaces the old per-chunk `_write_positional_delayed` tasks. Those
        were independently scheduled asyncio.sleep() calls with no ordering
        guarantee between them -- under load (TF/torch warmup, RAG calls,
        per-chunk pitch autocorrelation, etc. all sharing the event loop)
        two chunks' delayed writes could resolve out of order. Since
        audioop.ratecv's resample state is stateful and shared across the
        whole call, out-of-order writes corrupted it, and _write_positional's
        cursor logic would silently misplace a late chunk relative to
        already-written ones -- together producing exactly the kind of
        overlapping/garbled bot_audio seen in saved recordings.

        This worker drains a single FIFO queue, so chunks are always
        written in the same order _send_bot_pcm() sent them, each timed to
        land close to when the client should actually be playing it.

        Runs for the whole lifetime of the connection (started once in
        connect(), stopped once in disconnect()) -- NOT tracked via
        self._track()/self._active_tasks, so a per-turn _cancel_current_turn()
        (barge-in, interrupt, normal turn handoff) never accidentally kills
        this worker itself. Turn-scoped cancellation is instead handled by
        _drain_bot_write_queue(), which discards chunks that were never
        actually heard without tearing down the worker.
        """
        while True:
            try:
                item = await self._bot_write_queue.get()
            except asyncio.CancelledError:
                return
            if item is None:  # shutdown sentinel (not currently used, kept for safety)
                return
            chunk, write_at = item
            delay = write_at - time.time()
            if delay > 0:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
            try:
                await self._write_positional(
                    chunk, source_sample_rate=self.BOT_AUDIO_SAMPLE_RATE, source="bot"
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[REC] bot write worker failed to write chunk: {e}")
                # 🔥 NEW
                self._log_error("recording", "bot_write_worker", e,
                                severity="warning", chunk_bytes=len(chunk) if chunk else 0)

    def _drain_bot_write_queue(self):
        """🔥 NEW: discard any bot-audio chunks queued but not yet written.

        Called on barge-in/interrupt, right alongside
        _trim_recording_to_actual_playback(). Mirrors the old
        _write_positional_delayed()'s behaviour of never writing a chunk
        whose delayed task got cancelled before it fired ("it was never
        truly heard") -- except now, since the writer is a single
        persistent worker instead of one task per chunk, we can't just
        cancel a task; we drop the still-queued items directly instead.

        Without this, chunks queued right before a barge-in would still
        get written a moment later by the worker, silently re-extending
        recording["bot_audio"] past the point _trim_recording_to_actual_playback()
        just trimmed it back to.
        """
        drained = 0
        while True:
            try:
                self._bot_write_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            print(f"🧹 [REC] dropped {drained} queued-but-unheard bot audio chunk(s)")

    def _arm_bot_speaking_fallback(self, total_bytes, sample_rate, tag,
                                    min_delay=0.5, safety_buffer=1.5):
        """Schedule the fallback clear based on how much playback time is
        ACTUALLY still left, not a fixed guess made before anything was
        sent.

        🔥 UPDATED for real-time paced sends (see _send_bot_pcm): since
        audio is now paced to roughly real time as it's sent, by the time
        this is called (right after the pump/greeting loop finishes
        SENDING) most of a turn's nominal playback duration has already
        elapsed during those pacing sleeps -- only whatever still sits
        inside the PACED_SEND_LEAD_S window is genuinely unplayed. The
        OLD formula (total_bytes / sample_rate, i.e. "assume nothing has
        played yet") would double-count that already-elapsed time and
        leave bot_speaking stuck True for seconds longer than necessary
        whenever a real playback_end message is lost. self._playout_end
        (maintained by _send_bot_pcm) already tracks genuine remaining
        playback time, so prefer that; fall back to the old byte-estimate
        only if nothing was ever paced this turn (e.g. total_bytes is 0,
        or _send_bot_pcm was never called for some reason).

        🔥 NEW: safety_buffer raised from 0.6 -> 1.5. Observed calls show
        this fallback actually firing (i.e. playback_end genuinely never
        arriving) with a *further* ~1.3s wait on top of the old buffer --
        meaning the real client was taking noticeably longer to finish
        playback than the estimate assumed. Since this fallback gates VAD
        dispatch AND the recording's bot-audio write cursor, an estimate
        that fires too early causes the mic to start being treated as
        "normal listening" while the bot's audio is still genuinely
        playing on the client -- which is what produced the overlapping
        bot/user audio seen in saved recordings. This is a band-aid, not
        a fix: the real fix is making the client send playback_end
        reliably (see _clear_bot_speaking_after_delay's warning log).
        """
        self._cancel_bot_speaking_fallback()
        remaining = self._playout_end - time.time()
        if remaining <= 0:
            remaining = total_bytes / (sample_rate * 2) if sample_rate else 0.0
        delay = max(min_delay, remaining) + safety_buffer
        self._bot_speaking_clear_task = self._track(
            self._clear_bot_speaking_after_delay(delay, tag)
        )

    async def _cancel_current_turn(self):
        """Cancel any in-flight turn (process_utterance + its filler/sentence TTS
        send tasks) and WAIT for it to fully stop before doing anything else."""

        # 🔥 FIX: pehle sab pending TTS items ko cancel signal do,
        # warna unke background synthesis threads chalte rehte hain
        async with self._turn_items_lock:
            for item in self._turn_items:
                item["cancel_event"].set()

        pending = [t for t in self._active_tasks if not t.done()]
        if not pending:
            return
        print(f"🚫 [TURN] cancelling {len(pending)} in-flight task(s)")
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._current_process_task = None

    async def safe_send(self, text_data=None, bytes_data=None):
        """self.send() that never throws once the socket is gone, and never
        even tries once we know we're closed/cancelled."""
        if self._closed:
            return
        try:
            await self.send(text_data=text_data, bytes_data=bytes_data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._closed = True
            print(f"⚠️ [WS] send failed (client likely gone): {e}")

    async def receive(self, text_data=None, bytes_data=None):
        await self._ensure_call_session()
        if text_data:
            await self.handle_json(json.loads(text_data))
        elif bytes_data:
            await self.handle_audio(bytes_data)

    async def handle_json(self, data):
        msg_type = data.get("type")

        if msg_type == "init":
            await self._cancel_current_turn()
            self._cancel_bot_speaking_fallback()
            self.session["bot_speaking"] = False
            self.session["is_processing"] = False
            self._reset_barge_in_state()
            self._reset_playout_state()
            self._gnani_speech_end = False  # 🔥 NEW: don't leak a stale signal into the next call/turn
            self._track(self.send_greeting())

        elif msg_type == "playback_start":
            self.session["bot_speaking"] = True
            self._turn_start_time = time.time()   # 🔥 FIX 1
            print("🔊 [BOT] speaking")

        elif msg_type == "playback_end":
            self._cancel_bot_speaking_fallback()  # real signal arrived -- fallback not needed
            self.session["bot_speaking"] = False
            self._reset_barge_in_state()
            self._reset_playout_state()
            print("👂 [VAD] listening")

        elif msg_type == "interrupt":
            print("🚨 [INTERRUPT] user interrupted")
            # 🔥 NEW: same detection-instant capture as _trigger_barge_in --
            # this "interrupt" message IS the interruption signal here.
            interrupt_detected_at = time.time()
            self._cancel_bot_speaking_fallback()
            await self._cancel_current_turn()
            # 🔥 NEW: drop any bot-audio chunks still queued-but-unwritten
            # BEFORE trimming, so the worker can't write them a moment
            # later and silently re-extend past the trim point.
            self._drain_bot_write_queue()
            # 🔥 NEW: BARGE-IN RECORDING ACCURACY -- same backend-only trim
            # as the auto-detected barge-in path, see _trigger_barge_in.
            await self._trim_recording_to_actual_playback(cutoff_time=interrupt_detected_at)
            self.session["bot_speaking"] = False
            self.session["is_processing"] = False
            self._reset_barge_in_state()
            self._reset_playout_state()
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "bot_interrupted"}))

    VOICE_RMS_THRESHOLD = 300        # normal listening / no-echo-risk interrupts
    # 🔥 BARGE_IN_RMS_THRESHOLD is no longer read on the hot path -- the
    # live value is self._barge_in_rms_threshold, loaded from the DB
    # (LLMSetting.barge_in_threshold) once in connect(). Kept here only
    # as the documented fallback default (see BARGE_IN_DEFAULT_RMS at
    # module level, which mirrors this same value).
    BARGE_IN_RMS_THRESHOLD = 900
    BARGE_IN_SUSTAIN_DURATION = 0.5  
    BARGE_IN_FAST_MULTIPLIER = 2.2   
    BARGE_IN_FAST_SUSTAIN = 0.2      
    BARGE_IN_MIN_FRAMES = 2                                            
    BARGE_IN_MIN_TURN_AGE = 0.2      
                                                              
    BARGE_IN_COOLDOWN = 0.5                  

    # 🔥 PITCH + LOUDNESS BARGE-IN: RMS alone can't tell a horn/engine/TV
    # from a customer's voice -- these add a lightweight per-chunk pitch
    # check (autocorrelation, no new dependency) that must agree with the
    # loudness signal before barge-in fires. Not exposed to the customer
    # (see BARGE_IN_THRESHOLD_MIN_RMS/MAX_RMS at module level for the one
    # dial that IS customer-facing) -- these are internal accuracy tuning.
    BARGE_IN_PITCH_MIN_HZ = 100        # human voice fundamental lower bound
    BARGE_IN_PITCH_MAX_HZ = 300       # human voice fundamental upper bound
    BARGE_IN_PITCH_PERIODICITY_MIN = 0.30  # autocorrelation peak strength required to call a chunk "voiced"
    BARGE_IN_VOICE_RATIO_THRESHOLD = 0.40  # >=40% of loud frames in the sustain window must be voiced
                                            # (not 100% -- unvoiced consonants/pitch misses are normal in real speech)
    BARGE_IN_MAX_WINDOW_SECONDS = 1.5

    # 🔥 Pre-roll window: speech-start se pehle ka itna audio hamesha yaad
    # rakha jaata hai, taaki utterance ka onset na kate. 16kHz mono int16
    # => 32 bytes/ms, yaani 500ms = 16000 bytes. STTSession ke andar bhi
    # isi length ka apna ring hai (PersistentSarvamSTT.PREROLL_MS).
    PREROLL_MS = 500
    PREROLL_BYTES = 16000            # 500ms @ 16kHz mono int16

    def _estimate_pitch_hz(self, audio_bytes, sample_rate=16000):
        """Rough fundamental-frequency estimate via autocorrelation.

        Deliberately simple (pure numpy, no extra dependency) -- this is
        a per-chunk confirmation signal used alongside RMS, not a
        standalone voice classifier. See BARGE_IN_VOICE_RATIO_THRESHOLD
        for how individual chunk results get combined into a decision.
        """
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < 320:  # need ~20ms @16kHz minimum to estimate pitch at all
            return None

        samples = samples - np.mean(samples)
        if np.max(np.abs(samples)) < 1e-6:
            return None

        corr = np.correlate(samples, samples, mode='full')
        corr = corr[len(corr) // 2:]
        if corr[0] <= 0:
            return None

        min_lag = int(sample_rate / self.BARGE_IN_PITCH_MAX_HZ)  # higher freq -> shorter lag
        max_lag = int(sample_rate / self.BARGE_IN_PITCH_MIN_HZ)  # lower freq -> longer lag
        if max_lag >= len(corr) or min_lag >= max_lag:
            return None

        segment = corr[min_lag:max_lag]
        peak_idx = int(np.argmax(segment))
        peak_val = segment[peak_idx]
        if peak_val < self.BARGE_IN_PITCH_PERIODICITY_MIN * corr[0]:
            return None

        lag = min_lag + peak_idx
        if lag == 0:
            return None
        return sample_rate / lag

    async def _handle_barge_in_audio(self, audio_bytes, threshold=None):
        if not self._barge_in_enabled:
            return
        if self._barge_in_fired:
            return
        if self._call_ending:
            return

        now = time.time()
        if self._turn_start_time is not None and (now - self._turn_start_time) < self.BARGE_IN_MIN_TURN_AGE:
            return
        if now < self._barge_in_cooldown_until:
            return

        if threshold is None:
            threshold = self._barge_in_rms_threshold

        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples ** 2)) if len(samples) else 0.0

        # 🔥 DEBUG -- hata dena baad me
        # print(f"🔍 [BARGE-IN-DEBUG] rms={rms:.0f} threshold={threshold} "
        #     f"len_bytes={len(audio_bytes)}")

        if rms > threshold:
            is_voiced = self._estimate_pitch_hz(audio_bytes) is not None
            # print(f"🔍 [BARGE-IN-DEBUG] LOUD -- is_voiced={is_voiced} "
            #     f"frame_count={self._barge_in_frame_count} "
            #     f"voiced_count={self._barge_in_voiced_frame_count}")
            # Bahut loud voice = turant react karo (fast lane), halki si zyada
            # loud voice = normal sustain wait karo (false-positive se bachne ke liye).
            # 🔥 FIX 2: fast-lane multiplier raised so background spikes don't qualify.
            sustain_needed = (
                self.BARGE_IN_FAST_SUSTAIN
                if rms > threshold * self.BARGE_IN_FAST_MULTIPLIER
                else self.BARGE_IN_SUSTAIN_DURATION
            )

            # 🔥 PITCH + LOUDNESS: this chunk is loud enough -- now check if
            # it ALSO looks voice-pitched. We don't require every single
            # loud chunk to be voiced (unvoiced consonants like s/f/sh have
            # no pitch and are a normal part of real speech) -- instead we
            # track the ratio across the whole sustain window and decide
            # once the window closes. See BARGE_IN_VOICE_RATIO_THRESHOLD.
            self._barge_in_last_loud_time = now

            is_voiced = self._estimate_pitch_hz(audio_bytes) is not None

            self._barge_in_buffer.extend(audio_bytes)
            self._barge_in_frame_count += 1
            if is_voiced:
                self._barge_in_voiced_frame_count += 1

            if self._barge_in_voice_start is None:
                self._barge_in_voice_start = now
            elif now - self._barge_in_voice_start >= sustain_needed:
                voice_ratio = (
                    self._barge_in_voiced_frame_count / self._barge_in_frame_count
                    if self._barge_in_frame_count else 0.0
                )
                if (
                    self._barge_in_frame_count >= self.BARGE_IN_MIN_FRAMES  # 🔥 FIX 3
                    and voice_ratio >= self.BARGE_IN_VOICE_RATIO_THRESHOLD  # 🔥 pitch confirmation
                ):
                    buffered = bytes(self._barge_in_buffer)
                    self._reset_barge_in_state()
                    await self._trigger_barge_in(buffered)
                elif now - self._barge_in_voice_start >= self.BARGE_IN_MAX_WINDOW_SECONDS:
                    # Genuinely gave this window a fair chance (well beyond
                    # the normal sustain duration) and it still never became
                    # confidently voice-shaped -- safe to conclude this really
                    # is non-voice noise. Reset here, bounding memory on
                    # continuous loud noise (e.g. a long horn/engine sound).
                    self._reset_barge_in_state()
        else:
            # 🔥 FIX: single dip ignore karo, turant reset mat karo -- real speech
            # mein syllables ke beech natural rms dip hote hain. Sirf grace period
            # ke baad hi reset karo agar loudness genuinely gayab ho gayi.
            if self._barge_in_voice_start is not None:
                if not hasattr(self, '_barge_in_last_loud_time'):
                    self._barge_in_last_loud_time = self._barge_in_voice_start
                if now - self._barge_in_last_loud_time > 0.25:  # 250ms grace
                    self._reset_barge_in_state()
            else:
                self._reset_barge_in_state()

    async def _trigger_barge_in(self, buffered_audio):
        """Confirmed sustained user voice over the bot's own speech. Cancel
        the in-flight turn immediately and seed a fresh utterance capture
        with the audio we already buffered during the sustain window, so
        the start of what the user said isn't lost."""
        self._barge_in_fired = True
        barge_in_detected_at = time.time()
        self._barge_in_cooldown_until = barge_in_detected_at + self.BARGE_IN_COOLDOWN  # 🔥 FIX 4
        try:
            print("🚨 [BARGE-IN] sustained user voice over bot speech -- cancelling turn")
            self._cancel_bot_speaking_fallback()
            await self._cancel_current_turn()
            # 🔥 NEW: drop any bot-audio chunks still queued-but-unwritten
            # BEFORE trimming -- see _drain_bot_write_queue docstring.
            self._drain_bot_write_queue()
            # 🔥 NEW: BARGE-IN RECORDING ACCURACY -- trim whatever was
            # written-but-not-actually-played from this turn's bot_audio
            # before moving on. Backend-only, uses the detection timestamp
            # above, no client/dialer message needed.
            await self._trim_recording_to_actual_playback(cutoff_time=barge_in_detected_at)
            self.session["bot_speaking"] = False
            self.session["is_processing"] = False
            self._reset_playout_state()  # 🔥 NEW: turn is dead -- don't let stale pacing delay the next one
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "bot_interrupted"}))

            # Seed normal-listening state as if speech had just started.
            self._audio_buffer = bytearray(buffered_audio)
            self._speech_started = True
            self._speech_start_time = time.time() - self.BARGE_IN_SUSTAIN_DURATION
            self._last_voice_time = time.time()

            # 🔥 Reuses the persistent Sarvam connection opened in connect().
            # Pre-roll ring pehle discard karo -- barge-in par usme bot ke
            # bolne se pehle ka basi audio pada ho sakta hai, aur asli onset
            # hamare paas buffered_audio (sustain window) me already hai.
            self._stt_session.discard_preroll()
            self._stt_session.begin_turn()
            print(f"🎙️ [STT] backend={self._stt_session.backend}")
            if buffered_audio:
                self._stt_session.feed(buffered_audio)

            asyncio.create_task(
                database_sync_to_async(set_speech_state)(self.session_id, "speaking")
            )
        finally:
            self._barge_in_fired = False

    async def handle_audio(self, audio_bytes):
        # Recording start (pehli baar audio aayi)
        if not self.recording["active"]:
            self.recording["active"] = True
            self.recording["start_time"] = time.time()
            print(f"🔴 [RECORD] Started recording for session {self.session_id}")

        # 🔥 CHANGE: user audio ab single mixed buffer mein, real elapsed-time position par likha jaata hai
        await self._write_positional(audio_bytes, source_sample_rate=16000, source="user")

        # if self.session["is_processing"] or self.session["bot_speaking"]:
        if self.session["bot_speaking"]:
            # Bot audio is actually on the wire/playing -- real echo risk,
            # use the customer/DB-configured threshold (see connect()).
            # _handle_barge_in_audio() itself checks self._barge_in_enabled
            # first and no-ops entirely if the toggle is off.
            await self._handle_barge_in_audio(audio_bytes, threshold=self._barge_in_rms_threshold)
            return

        if self.session["is_processing"]:
            await self._handle_barge_in_audio(audio_bytes, threshold=self._adaptive_threshold)  # ✅ adaptive
            return

        if not hasattr(self, '_audio_buffer'):
            self._audio_buffer = bytearray()
            self._last_voice_time = None       # voice kab pehli baar detect hui
            self._speech_started = False

        self._audio_buffer.extend(audio_bytes)

        # 🔥 FIX: jab tak speech start nahi hui, buffer ko sirf PREROLL_BYTES
        # tak trim karte raho. Pehle ye buffer call ke shuru se hi badhta
        # rehta tha, isliye utterance_bytes me minaton ka silence chala jaata
        # tha (Google rescue/batch path ke liye bekaar payload aur memory).
        if not self._speech_started and len(self._audio_buffer) > self.PREROLL_BYTES:
            del self._audio_buffer[:len(self._audio_buffer) - self.PREROLL_BYTES]

        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples ** 2)) if len(samples) else 0.0

        if not self._speech_started and rms < self._adaptive_threshold * 2:
            alpha = 0.02   # dheera-dheera update, jhatka nahi
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * rms
            self._adaptive_threshold = max(200.0, self._noise_floor * 3.0)

        # 🔥 NEW: this is the ONLY place in the normal listening path where
        # rms was previously invisible in the logs -- _handle_barge_in_audio
        # above prints its own rms, but plain listening (this branch) never
        # did. When a call goes quiet after a turn and nothing dispatches,
        # this line is what tells you whether audio stopped arriving at all
        # vs. arrived but never crossed _adaptive_threshold.
        if DEBUG_VAD_RMS:
            print(f"🔍 [VAD-DEBUG] rms={rms:.0f} adaptive_threshold={self._adaptive_threshold:.0f} "
                  f"noise_floor={self._noise_floor:.0f} speech_started={self._speech_started} "
                  f"len_bytes={len(audio_bytes)}")

        SILENCE_HANGOVER = 0.6          # thoda badhaya 0.4→0.5, false-cutoff kam karega
        # 🔥 FIX: 0.3s/0.5s ke purane gates chhote utterances ("hello", "haan",
        # "ji") ko poora drop kar dete the -- wo turn kabhi dispatch hi nahi
        # hota tha, isliye lagta tha ki STT ne khaali transcript diya.
        MIN_SPEECH_DURATION = 0.15      # ~150ms voice kaafi hai
        MIN_BUFFER_BYTES = 8000         # 0.25s @16kHz — sirf noise burst reject karne ke liye

        now = time.time()

        # 🔥 FIX: STT ko ab HAR chunk diya jaata hai, VAD gate se PEHLE.
        # STTSession.feed() khud decide karta hai: turn active nahi hai to
        # audio uske 500ms pre-roll ring me jaata hai (socket par nahi), aur
        # begin_turn() par wahi ring Sarvam ko flush hoti hai. Pehle feed()
        # sirf speech-start ke BAAD hota tha, isliye har word ka onset kat
        # jaata tha -- aur Sarvam ka apna server-side VAD usko dobara trim
        # karta tha, jiski wajah se chhoti baatein empty transcript deti thi.
        if self._stt_session:
            self._stt_session.feed(audio_bytes)

        if rms > self._adaptive_threshold:
            if not self._speech_started:
                self._speech_start_time = now
                self._speech_started = True
                print("🎙️ [VAD] speech start")

                # begin_turn() pre-roll ring ko turant socket par flush karta hai
                self._stt_session.begin_turn()
                print(f"🎙️ [STT] backend={self._stt_session.backend}")

                asyncio.create_task(
                    database_sync_to_async(set_speech_state)(self.session_id, "speaking")
                )
            self._last_voice_time = now

        if self._speech_started and self._last_voice_time:
            silence_duration = now - self._last_voice_time
            speech_duration = self._last_voice_time - self._speech_start_time

            # 🔥 LATENCY: dispatch immediately if the STT backend's own
            # server-side VAD already signaled speech-end -- don't also
            # wait out the full local SILENCE_HANGOVER on top of it. Falls
            # back to the local timer unchanged if the signal never
            # arrives (backend down / per-utterance Google fallback path).
            gnani_ended = self._gnani_speech_end
            if (
                (gnani_ended or silence_duration > SILENCE_HANGOVER)
                    and speech_duration >= MIN_SPEECH_DURATION
                    and len(self._audio_buffer) >= MIN_BUFFER_BYTES):
                if gnani_ended:
                    print(f"⚡ [VAD] speech-end signal — early dispatch "
                          f"(local silence was only {silence_duration:.0f}ms)")
                stt_session = self._stt_session  # persistent session -- stays alive across turns
                utterance_bytes = bytes(self._audio_buffer)
                self._audio_buffer = bytearray()
                self._speech_started = False
                self._last_voice_time = None
                self._gnani_speech_end = False  # 🔥 NEW: don't leak into next turn
                # NOTE: self._stt_session is NOT reset here -- it's a
                # persistent, call-scoped connection now (see connect()),
                # not a per-utterance object. Its per-turn state gets reset
                # by begin_turn() at the next speech-start instead.

                # 🔥 FIX: this is where the turn actually needs to kick off --
                # previously the utterance was captured but never dispatched
                # to process_utterance, so nothing happened after silence.
                await self._cancel_current_turn()
                self._current_process_task = self._track(
                    self.process_utterance(utterance_bytes, stt_session)
                )

    # ============================================================
    # 🔥 NAYA: Real elapsed-time positional write — but ab HAR source
    # ka apna alag buffer hai, isliye additive mixing/clipping nahi hoti.
    # ============================================================
    def _advance_bot_play_cursor(self, now: float):
        """🔥 BARGE-IN RECORDING ACCURACY -- backend-only simulation of
        "how far has the customer actually heard into
        recording['bot_audio']", maintained CONTINUOUSLY across the whole
        call (not reset per turn).

        Caller must hold self._audio_lock (matches _write_positional's
        locking -- both read/mutate self._write_cursor["bot"]).
        """
        bytes_per_sec = self.RECORD_SAMPLE_RATE * 2
        write_cursor = self._write_cursor.get("bot", 0)

        if self._bot_play_anchor_time is None:
            # Nothing has ever started playing yet.
            self._bot_play_anchor_time = now
            self._bot_play_anchor_offset = write_cursor
            self._bot_play_position = write_cursor
            return

        elapsed = max(0.0, now - self._bot_play_anchor_time)
        theoretical = self._bot_play_anchor_offset + int(elapsed * bytes_per_sec)
        position = min(theoretical, write_cursor)
        self._bot_play_position = position

        if position >= write_cursor:
            # Caught up to everything sent so far -- re-anchor HERE so a
            # coming gap (waiting on LLM/TTS for the next chunk) doesn't
            # get miscounted as "played" once new audio starts flowing.
            self._bot_play_anchor_time = now
            self._bot_play_anchor_offset = position

    async def _write_positional(self, pcm_bytes: bytes, source_sample_rate: int, source: str):
        if not self.recording.get("start_time") or not pcm_bytes:
            return

        if source_sample_rate != self.RECORD_SAMPLE_RATE:
            if len(pcm_bytes) % 2 != 0:
                pcm_bytes = pcm_bytes + b'\x00'
            pcm_bytes, self._bot_resample_state = audioop.ratecv(
                pcm_bytes, 2, 1, source_sample_rate, self.RECORD_SAMPLE_RATE,
                self._bot_resample_state
            )

        buf_key = "user_audio" if source == "user" else "bot_audio"

        async with self._audio_lock:
            elapsed = time.time() - self.recording["start_time"]
            if elapsed < 0:
                elapsed = 0

            bytes_per_sec = self.RECORD_SAMPLE_RATE * 2
            wallclock_offset = int(elapsed * bytes_per_sec)
            if wallclock_offset % 2 != 0:
                wallclock_offset -= 1

            cursor = self._write_cursor.get(source, 0)
            offset = wallclock_offset if wallclock_offset > cursor else cursor

            if source == "bot":
                now = time.time()
                if offset > cursor:
                    self._bot_play_position = offset
                    self._bot_play_anchor_time = now
                    self._bot_play_anchor_offset = offset
                else:
                    self._advance_bot_play_cursor(now)

            buf = self.recording[buf_key]
            needed_len = offset + len(pcm_bytes)
            if len(buf) < needed_len:
                buf.extend(b'\x00' * (needed_len - len(buf)))

            # 🔥 Sirf overwrite — apne hi alag channel mein likh rahe hain,
            # kisi doosre source ke saath add/clip karne ki zaroorat hi nahi
            buf[offset:offset + len(pcm_bytes)] = pcm_bytes

            self._write_cursor[source] = offset + len(pcm_bytes)

    async def _trim_recording_to_actual_playback(self, cutoff_time=None):
        """🔥 BARGE-IN RECORDING ACCURACY (backend-only, dialer-agnostic).

        Called from _trigger_barge_in() and the explicit "interrupt"
        handler, both of which already detect the interruption purely
        server-side (mic RMS / VAD) -- so this works unchanged for any
        future dialer, no frontend cooperation required.

        Uses the continuous play-cursor simulator (_advance_bot_play_cursor)
        instead of a single per-turn elapsed-wall-clock-time guess, so it
        correctly survives generation gaps mid-turn and TTS chunks sent in
        a burst faster than real-time -- see that method's docstring.

        NOTE: callers should call _drain_bot_write_queue() BEFORE this, so
        any bot-audio chunks still sitting in the (now-obsolete) write
        queue get dropped instead of being written moments later and
        silently re-extending recording["bot_audio"] past this trim point.
        """
        now = cutoff_time if cutoff_time is not None else time.time()
        lead_bytes = int((self.PLAYBACK_LEAD_MS / 1000.0) * self.RECORD_SAMPLE_RATE * 2)

        async with self._audio_lock:
            self._advance_bot_play_cursor(now)
            write_cursor = self._write_cursor.get("bot", 0)
            cutoff = max(0, self._bot_play_position - lead_bytes)

            if cutoff < write_cursor:
                trimmed = write_cursor - cutoff
                self.recording["bot_audio"][cutoff:write_cursor] = b'\x00' * trimmed
                # Pull the write cursor back too, so the next audio written
                # doesn't leave a phantom silent gap where this unheard
                # tail used to be.
                self._write_cursor["bot"] = cutoff
                print(f"✂️ [RECORD] Trimmed {trimmed} bytes "
                      f"(~{trimmed / (self.RECORD_SAMPLE_RATE * 2) * 1000:.0f}ms) of "
                      f"unheard bot audio from recording (barge-in)")
                self._bot_play_position = cutoff
                self._bot_play_anchor_time = now
                self._bot_play_anchor_offset = cutoff

    async def _tts_producer_queue(self, text, cancel_event, use_cache=False):
        """Sirf synthesis background thread mein start karta hai, chunks queue mein daalta hai.
        use_cache=True aur is text ka pre-generated PCM maujood hai toh seedha
        wahi file chunk karke queue mein daal do -- TTS engine call hi nahi hota.

        🔥 CHANGED: ab (queue, was_cached) tuple return karta hai. was_cached
        ka ek hi kaam hai -- TTS ka paisa lagaana hai ya nahi. Cached PCM
        par Murf ko koi request nahi jaati, isliye uska cost 0 hai.
        Caller: _schedule_tts().
        """
        loop = asyncio.get_event_loop()
        q = asyncio.Queue()

        if use_cache:
            cached = load_cached_pcm(text)
            if cached:
                print(f"⚡ [FILLER-CACHE] HIT — playing cached PCM ({len(cached)} bytes) for '{text[:40]}'")
                def producer_cached():
                    CHUNK = 4096
                    for i in range(0, len(cached), CHUNK):
                        if cancel_event.is_set():
                            break
                        loop.call_soon_threadsafe(q.put_nowait, cached[i:i + CHUNK])
                    loop.call_soon_threadsafe(q.put_nowait, None)
                loop.run_in_executor(None, producer_cached)
                return q, True                  # 🔥 cached -- koi TTS cost nahi
            # not cached -- fall through to live synthesis below
            print(f"🐢 [FILLER-CACHE] MISS — synthesizing live for '{text[:40]}'")

        tts = get_tts_service()

        def producer():
            try:
                for chunk in tts.synthesize_stream(text):
                    if cancel_event.is_set():
                        break
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                # 🔥 NEW: Murf down / rate-limited / network error. Pehle ye
                # exception is background thread me chupchaap gum ho jaata
                # tha (finally sirf sentinel daal deta tha) -- turn silently
                # bina audio ke khatam ho jaata. Ab DB me row banti hai.
                print(f"❌ [TTS] synthesize_stream failed: {e}")
                logger.error(f"[TTS] synthesize_stream failed: {e}")
                loop.call_soon_threadsafe(
                    self._log_error, "tts", "synthesize_stream", e,
                )
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(None, producer)
        return q, False                         # 🔥 live synthesis -- cost lagega

    async def _schedule_tts(self, text, is_first_sentence=False, use_cache=False, is_filler=False):
        cancel_event = threading.Event()
        # 🔥 CHANGED: _tts_producer_queue ab (queue, was_cached) deta hai.
        queue, was_cached = await self._tts_producer_queue(text, cancel_event, use_cache=use_cache)
        if not was_cached:
            # 🔥 NEW: sirf live-synthesized text ke characters count hote hain.
            # Ye turn ke aakhir me cost_tts() ke through save_turn() me
            # jaata hai -- dekho process_utterance() / _speak_deterministic_turn().
            self._turn_tts_chars += len(text or "")
        item = {
            "queue": queue,
            "cancel_event": cancel_event,
            "is_first_sentence": is_first_sentence,
            "is_filler": is_filler,          # 🔥 NEW
            "start_time": time.time(),
        }
        async with self._turn_items_lock:
            self._turn_items.append(item)
        return item

    def _consume_tts_chars(self):
        """🔥 NEW: is turn ke TTS characters lo aur counter reset kar do.

        Greeting ke chars bhi isi me jud jaate hain -- greeting ka apna
        koi ConversationTurn row nahi banta (wo CallSession banne se
        PEHLE bolti hai), isliye uska paisa PEHLE bot turn ke saath DB
        me jaata hai. Ek hi baar -- _greeting_tts_chars yahin 0 ho jaata.
        """
        total = self._turn_tts_chars + self._greeting_tts_chars
        if self._greeting_tts_chars:
            print(f"💰 [COST] greeting ke {self._greeting_tts_chars} TTS chars "
                  f"is turn me jod diye")
        self._turn_tts_chars = 0
        self._greeting_tts_chars = 0
        return total

    async def _audio_pump(self, timing_tracker):
        """🔥 EK HI jagah jahan bot ka audio bheja jaata hai. Sentences ko
        strictly us order mein bhejta hai jis order mein schedule hue the --
        isliye do sentences ki audio kabhi ek dusre ke beech mein nahi ghusti."""
        idx = 0
        while True:
            while True:
                async with self._turn_items_lock:
                    if idx < len(self._turn_items):
                        item = self._turn_items[idx]
                        break
                    if self._turn_items_done:
                        return
                await asyncio.sleep(0.01)

            first = True
            tts_start = item["start_time"]
            while True:
                chunk = await item["queue"].get()
                if chunk is None:
                    break
                if item["cancel_event"].is_set():
                    continue

                if chunk:
                    timing_tracker['tts_total_bytes'] = timing_tracker.get('tts_total_bytes', 0) + len(chunk)
                    if first:
                        if timing_tracker.get('tts_first_audio_ms') is None:
                            timing_tracker['tts_first_audio_ms'] = (time.time() - tts_start) * 1000
                        if item["is_first_sentence"] and 'real_user_heard_at' not in timing_tracker:
                            timing_tracker['real_user_heard_at'] = time.time()
                        if item["is_filler"] and 'filler_first_chunk_at' not in timing_tracker:
                            timing_tracker['filler_first_chunk_at'] = time.time()
                        first = False
                    # 🔥 UPDATED: was a raw _write_positional() + safe_send()
                    # pair sent as fast as chunks arrived -- now paced to
                    # real time via _send_bot_pcm() so barge-in actually
                    # has something left to cut off. See _send_bot_pcm().
                    await self._send_bot_pcm(chunk)
            idx += 1

    async def _llm_stream_async(self, session_id, customer_text, context, filler_text=None,
                                 reference_context=None, use_rag=True, history=None):   # 🆕 history
        """Sync generator ko async queue me convert karo taaki event loop free rahe"""
        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def producer():
            try:
                for chunk in chat_turn_stream(
                    session_id=session_id, customer_text=customer_text,
                    context=context, use_rag=use_rag, filler_text=filler_text,
                    reference_context=reference_context,
                    history=history or [],          # 🆕 service ko history thama do
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                # 🔥 NEW: LLM provider down / timeout / bad response. Pehle
                # ye exception thread me gum ho jaata tha aur turn ke paas
                # khaali full_response reh jaata -- bot "chup" ho jaata.
                print(f"❌ [LLM] chat_turn_stream failed: {e}")
                logger.error(f"[LLM] chat_turn_stream failed (session={session_id}): {e}")
                loop.call_soon_threadsafe(
                    self._log_error, "llm", "chat_turn_stream", e,
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, producer)

        while True:
            chunk = await queue.get()   # ← yeh REAL await hai, event loop free rehta hai
            if chunk is None:
                break
            if self._closed:
                break
            yield chunk

    async def _score_and_persist_turn(self, session_id, customer_text, bot_response_text,
                                       filler_text, turn_prompt_tokens, turn_output_tokens,
                                       bot_turn_task):
        """
        🔥 Background, non-blocking per-turn scoring -- fired via
        _fire_and_forget from process_utterance, never awaited on the hot
        path, so it adds zero latency to what the customer hears.

        turn_prompt_tokens/turn_output_tokens are captured SYNCHRONOUSLY
        by the caller (see get_last_turn_usage() in process_utterance),
        not re-read here -- by the time this background task actually
        runs, the shared client singleton may already belong to a
        different session's turn.

        Waits on bot_turn_task (the save_turn() task for this turn's bot
        row) first, so scores attach to the correct ConversationTurn.
        """
        try:
            bot_turn = await bot_turn_task
        except Exception as e:
            logger.error(f"[SCORE] save_turn task failed, cannot score turn (session={session_id}): {e}")
            self._log_error("db", "save_turn(bot)", e, severity="error")   # 🔥 NEW
            return
        if not bot_turn:
            logger.warning(f"[SCORE] save_turn returned no row, skipping scoring (session={session_id})")
            return

        try:
            turn_scores = await asyncio.to_thread(
                score_and_price_turn, customer_text, bot_response_text, filler_text,
                turn_prompt_tokens, turn_output_tokens,
            )
        except Exception as e:
            logger.error(f"[SCORE] score_and_price_turn failed (session={session_id}): {e}")
            self._log_error("llm", "score_and_price_turn", e, severity="warning",
                            turn_id=bot_turn.id)                            # 🔥 NEW
            return

        try:
            await database_sync_to_async(save_turn_scores)(
                bot_turn.id,
                accuracy=turn_scores.get("accuracy"),
                filler_accuracy=turn_scores.get("filler_accuracy"),
                llm_pricing=turn_scores.get("llm_pricing"),
            )
        except Exception as e:
            logger.error(f"[SCORE] save_turn_scores failed (session={session_id}, turn_id={bot_turn.id}): {e}")
            self._log_error("db", "save_turn_scores", e, severity="error",
                            turn_id=bot_turn.id)                            # 🔥 NEW

    async def _speak_deterministic_turn(self, text, timing, transcript, turn_record):
        """
        🔥 Speaks a fixed, Python-built line WITHOUT going through the LLM
        at all -- used for turns where the response text must be built
        deterministically rather than generated (e.g. call-ending / callback
        closing lines).

        Mirrors the tail of the normal LLM path (schedule TTS → pump audio
        → save the turn/transcript with timing → send the same ai_response/
        pcm_end/done frames the client already expects) so from the
        frontend's point of view this looks like any other turn.

        NOTE: no score_and_price_turn() call here -- no real LLM generation
        happened this turn, so there's nothing to score/price. accuracy/
        filler_accuracy/llm_pricing stay at their model defaults (None/0)
        for these rows, same as before this feature existed.

        🔥 TTS ka paisa phir bhi lagta hai -- ye line Murf se hi boli
        jaati hai, isliye tts_pricing yahan bhi save hota hai.
        """
        self._turn_items = []
        self._turn_items_done = False
        self._turn_tts_chars = 0        # 🔥 NEW: is turn ka fresh count
        pump_task = self._track(self._audio_pump(timing))

        await self._schedule_tts(text, is_first_sentence=True)

        self._turn_items_done = True
        await pump_task

        turn_record["bot"] = text
        self.recording["transcript"].append(turn_record)
        print(f"📝 [TRANSCRIPT] Turn saved (deterministic/no-LLM): "
              f"User='{transcript[:30]}...' Bot='{text[:30]}...'")

        asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, False))

        # No real LLM call happened -- fill the timing keys build_timing_record()
        # expects so its ms() math (which needs both a start and end key) doesn't
        # silently produce None across the board for this turn.
        now = time.time()
        timing.setdefault('llm_first_token_at', now)
        timing['llm_complete_at'] = now
        timing['user_heard_at'] = now
        
        # 🔥 Redis history me bhi save -- warna agle turn par LLM ko pata
        # nahi chalega ki bot ne booking confirm/closing line boli thi.
        await database_sync_to_async(save_conversation)(self.session_id, "Aarohi", text)

        # 🔥 NEW: TTS cost. _consume_tts_chars() greeting ke bache hue
        # chars bhi is turn me jod deta hai (agar pehla bot turn yahi hai).
        turn_tts_cost = cost_tts(self._consume_tts_chars())

        asyncio.create_task(
            database_sync_to_async(save_turn)(
                self.session_id, "bot", text,
                timing=build_timing_record(timing),
                tts_pricing=turn_tts_cost,          # 🔥 NEW
            )
        )

        await self.safe_send(text_data=json.dumps({
            "type": "ai_response", "text": text, "back_flag": 2, "usage": "0",
        }))
        await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
        await self.safe_send(text_data=json.dumps({"type": "done"}))

    async def _end_call_turn(self, transcript, timing, turn_record):
        """
        🔥 NEW: fires when classify_intent() returns "call_ending" -- the
        customer wants to hang up. Mirrors _speak_deterministic_turn's
        no-LLM pattern: speak one fixed closing line, then close
        the socket ourselves instead of waiting for the client/customer to
        do it.

        We don't duplicate any DB/recording bookkeeping here -- closing the
        socket makes Channels invoke disconnect() the same as it would for
        an ordinary client-initiated hangup, and disconnect() already saves
        the recording, marks the CallSession "completed" (has_conversation
        is already True by this point in process_utterance), and fires the
        post-call summary. This method's only job is to speak the line and
        close.
        """
        closing_text = filler_for_intent("call_ending", transcript=transcript)
        print(f"👋 [END-CALL] intent=call_ending -- speaking closing line, then closing the socket")

        try:
            await self._speak_deterministic_turn(closing_text, timing, transcript, turn_record)

            await self.safe_send(text_data=json.dumps({"type": "call_ended"}))
        finally:
            self._closed = True
            print(f"🔌 [END-CALL] server-initiated close for session {self.session_id}")
            try:
                await self.close(code=1000)
            except Exception as e:
                print(f"⚠️ [END-CALL] close() raised (socket likely already gone): {e}")

    async def _callback_turn(self, transcript, timing, turn_record):
        """
        🔥 NEW: fires when classify_intent() returns "callback" -- customer
        wants us to call back later instead of continuing now. Records the
        request (plain JSON file, same shape as booking_tools.py) via
        schedule_callback(), speaks one fixed confirmation line, then closes
        the socket -- mirrors _end_call_turn()'s pattern exactly.
        """
        context = self.session.get("cloud_context") or {}
        customer_name = context.get("customer_name") or "Customer"

        try:
            result = await asyncio.to_thread(
                schedule_callback,
                session_id=self.session_id,
                phone_number=self.phone_number,
                customer_name=customer_name,
                reason=transcript,
            )
            print(f"📞 [CALLBACK] scheduled: {result}")
        except Exception as e:
            # 🔥 NEW: callback store fail hua -- customer ko phir bhi
            # closing line bolni hai (usne request kiya hai), par record
            # me jaana chahiye ki save nahi hua.
            print(f"❌ [CALLBACK] schedule_callback failed: {e}")
            logger.error(f"[CALLBACK] schedule_callback failed (session={self.session_id}): {e}")
            self._log_error("booking", "schedule_callback", e, severity="error",
                            phone=self.phone_number)

        closing_text = "ठीक है जी, कोई बात नहीं। मैं आपको कल कॉलबैक कर लूंगी। धन्यवाद, नमस्ते।"
        print("👋 [CALLBACK] intent=callback -- speaking closing line, then closing the socket")

        try:
            await self._speak_deterministic_turn(closing_text, timing, transcript, turn_record)
            await self.safe_send(text_data=json.dumps({"type": "call_ended"}))
        finally:
            self._closed = True
            print(f"🔌 [CALLBACK] server-initiated close for session {self.session_id}")
            try:
                await self.close(code=1000)
            except Exception as e:
                print(f"⚠️ [CALLBACK] close() raised (socket likely already gone): {e}")

    async def process_utterance(self, audio_bytes, stt_session=None):
        """Streaming-STT → Cloud LLM → TTS Streaming with FULL timing tracking"""
        self.session["is_processing"] = True
        self._turn_tts_chars = 0        # 🔥 NEW: har turn fresh TTS char count

        # 🔥 TIMING TRACKER
        timing = {
            'audio_received_at': time.time(),
            'stt_done_at': None,
            'llm_first_token_at': None,
            'llm_complete_at': None,
            'tts_first_audio_ms': None,
            'tts_total_ms': None,
            'tts_total_bytes': 0,
            'user_heard_at': None,
        }

        try:
            # ============================================================
            # STEP 1: STT (Speech-to-Text) — flush the live streaming
            # session that's been running since VAD detected speech start.
            # Falls back to the 60db batch path if no session was passed
            # in (keeps this method safe to call standalone, unchanged).
            # ============================================================
            print(f"\n{'='*60}")
            print(f"🎤 [TURN START] New user turn")
            print(f"{'='*60}")
            pump_task = None

            if stt_session is not None:
                # 🔥 No thread, no reconnect -- flush + await the result on
                # the already-open persistent connection (see stt_service.py).
                #
                # rescue_audio: agar Sarvam khaali laut aaye ya turn ke beech
                # me socket mar jaaye, to STTSession wahi utterance ek baar
                # Google par retry kar leta hai. Iske bina wo poora turn gum
                # ho jaata tha aur user ko dobara bolna padta tha.
                #transcript = await stt_session.end_turn(rescue_audio=audio_bytes)
                speech_duration_estimate = len(audio_bytes) / (16000 * 2)
                gnani_timeout = min(1.5, max(0.5, speech_duration_estimate + 0.4))
                try:
                    transcript = await stt_session.end_turn(rescue_audio=audio_bytes, timeout=gnani_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 🔥 NEW: STT provider down / socket dead / rescue bhi
                    # fail. Turn khaali transcript ke saath aage badhega
                    # (neeche wala `if not transcript` handle karega), par
                    # DB me pata chalega ki kaun gira.
                    print(f"❌ [STT] end_turn failed: {e}")
                    logger.error(f"[STT] end_turn failed (session={self.session_id}): {e}")
                    self._log_error("stt", "end_turn", e, severity="error",
                                    backend=getattr(stt_session, "backend", None),
                                    audio_bytes=len(audio_bytes))
                    transcript = ""
            else:
                transcript = await self.do_stt(audio_bytes)

            timing['stt_done_at'] = time.time()
            stt_latency = (timing['stt_done_at'] - timing['audio_received_at']) * 1000

            if stt_session is not None and stt_session.first_result_time and stt_session.start_time:
                timing['stt_first_token_ms'] = round(
                    (stt_session.first_result_time - stt_session.start_time) * 1000, 1
                )
            else:
                timing['stt_first_token_ms'] = None

            if not transcript:
                backend_used = getattr(stt_session, "backend", None) if stt_session else None
                print(f"❌ [STT] No transcript found (backend={backend_used})")
                # 🔥 Previously returned silently -- a client (or real caller's
                # app) waiting on a "done"/"error" event would hang until its
                # own timeout. Always signal the turn ended.
                await self.safe_send(text_data=json.dumps({
                    "type": "no_speech",
                    "message": "no transcript detected"
                }))
                await self.safe_send(text_data=json.dumps({"type": "done"}))
                return

            print(f"📝 [STT] '{transcript}'")
            print(f"⏱️ [STT] Audio → Text: {stt_latency:.0f}ms")

            # 🔥 NEW: Mark conversation happened
            self.session["has_conversation"] = True

            # 🔥 NEW: Turn record create karo (abhi sirf user ka text)
            turn_record = {
                "timestamp": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
                "user": transcript,
                "bot": "",
            }

            asyncio.create_task(
                database_sync_to_async(set_final_transcript)(self.session_id, transcript)
            )
            # 🔥 DB: persist the customer's turn
            # 🔥 NEW: STT cost isi row par -- customer turn = jitna audio
            # transcribe hua. audio_bytes PCM16 mono @16kHz hai.
            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "customer", transcript,
                    stt_pricing=cost_stt_from_bytes(len(audio_bytes)),      # 🔥 NEW
                )
            )
            # 🔥 ORDER MATTERS: history PEHLE fetch, phir current message save.
            # Ulta kiya to current message do baar jaayega LLM ke paas.
            llm_history = await database_sync_to_async(get_history_for_llm)(self.session_id)
            await database_sync_to_async(save_conversation)(
                self.session_id, "Customer", transcript
            )

            await self.safe_send(text_data=json.dumps({"type": "transcript", "text": transcript}))
            await self.safe_send(text_data=json.dumps({"type": "pcm_start"}))
            self.session["bot_speaking"] = True
            self._turn_start_time = time.time()   # 🔥 FIX 1

            # ============================================================
            # STEP 1.4: SERVICE BOOKING (plain Python, not the LLM)
            #
            # Same logic/flow as dialer.py's ExotelDialerConsumer: availability
            # + the actual reservation write are deterministic DB operations
            # in views_admin.py; this handler only decides WHEN to call them
            # (simple keyword checks).
            #
            # 🔥 Option B fix: the four booking sub-cases below now speak a
            # fixed Python-built line (deterministic_response_text) and skip
            # the LLM ENTIRELY for this turn, instead of feeding a hint into
            # booking_reference_context and trusting the LLM to phrase it
            # correctly. Previously the LLM sometimes announced "booking
            # confirmed" on the very turn Python only marked the slot as
            # PENDING (not yet booked). Now the words "confirmed"/"booked"
            # can only ever be spoken once book_slot_for_session() has
            # actually returned success=True.
            #
            # The only case still routed through the LLM is "customer asked
            # about a date but that exact slot was already taken" -- that's
            # a plain FYI + alternative-time suggestion, not a confirmation
            # claim, so natural LLM phrasing is fine there and it still
            # reuses booking_reference_context as before.
            # ============================================================
            booking_reference_context = None
            deterministic_response_text = None  # if set, bypass the LLM for this turn
            pending = self.session.get("pending_slot")
            customer_name = (self.session.get("cloud_context") or {}).get("customer_name") or "आप"

            if pending and mentions_confirmation(transcript):
                booking_result = await database_sync_to_async(book_slot_for_session)(
                    self.session_id, pending["date"], pending["time"],
                )
                self.session["pending_slot"] = None
                if booking_result.get("success"):
                    # 🔥 Only reachable once the DB row genuinely exists.
                    deterministic_response_text = (
                        f"जी हाँ {customer_name} जी, आपका सर्विस अपॉइंटमेंट "
                        f"{_format_date_hi(pending['date'])} को {_format_time_hi(pending['time'])} "
                        f"के लिए कन्फर्म हो गया है। धन्यवाद!"
                    )
                elif booking_result.get("error") == "slot_taken":
                    alt = booking_result.get("next_available")
                    if alt:
                        deterministic_response_text = (
                            f"माफ़ कीजिए {customer_name} जी, वो स्लॉट अभी-अभी किसी और ने बुक कर लिया। "
                            f"उसी दिन {_format_time_hi(alt)} खाली है -- क्या मैं वो कन्फर्म कर दूं?"
                        )
                        self.session["pending_slot"] = {"date": pending["date"], "time": alt}
                    else:
                        deterministic_response_text = (
                            f"माफ़ कीजिए {customer_name} जी, वो स्लॉट अभी-अभी बुक हो गया और उस दिन "
                            f"और कोई स्लॉट खाली नहीं है। क्या आप कोई और दिन बताना चाहेंगे?"
                        )
                else:
                    # 🔥 NEW: booking_failed / invalid date / no_branch --
                    # customer ko maafi wali line jaati hai, aur error DB me.
                    self._log_error(
                        "booking", "book_slot_for_session",
                        booking_result.get("error") or "unknown booking failure",
                        severity="error",
                        slot_date=pending.get("date"), slot_time=pending.get("time"),
                    )
                    deterministic_response_text = (
                        f"माफ़ कीजिए {customer_name} जी, अभी बुकिंग में कोई तकनीकी दिक्कत आ गई। "
                        f"क्या हम थोड़ी देर में दोबारा कोशिश करें?"
                    )
            else:
                slot_date, slot_time, slot_time_was_rounded = extract_slot_request(transcript)

                # 🔥 Continuation fallback: if this turn didn't look
                # booking-related on its own (extract_slot_request is gated
                # on mentions_booking(), and a bare reply like "11 baje
                # thik hai" has no booking keyword in it at all), but we're
                # mid-way through asking for a date/time from an earlier
                # turn, try to resolve it as a continuation of that pending
                # question instead of silently dropping it.
                awaiting_date = self.session.get("awaiting_slot_date")
                if slot_date is None and awaiting_date:
                    slot_date, slot_time, slot_time_was_rounded = extract_slot_continuation(
                        transcript, known_date=awaiting_date,
                    )

                if slot_date:
                    # Remember this date is "in progress" so the NEXT turn
                    # (even a bare time answer with no booking keyword) can
                    # still be resolved against it via the fallback above.
                    self.session["awaiting_slot_date"] = slot_date.isoformat()

                    slots = await database_sync_to_async(get_available_slots)(slot_date)
                    if slot_time:
                        match = next((s for s in slots if s["time"] == slot_time), None)
                        if match and match["status"] == "open":
                            # Hold it as pending -- only actually booked once the
                            # customer verbally confirms on the NEXT turn, via the
                            # `pending and mentions_confirmation(...)` branch above.
                            self.session["pending_slot"] = {
                                "date": slot_date.isoformat(), "time": slot_time,
                            }
                            self.session["awaiting_slot_date"] = None  # resolved
                            # 🔥 The confirm-ask itself is deterministic too -- this
                            # is the exact question mentions_confirmation() on the
                            # NEXT turn is checking the reply against, so it must
                            # actually be asked, not left to the LLM's discretion.
                            if slot_time_was_rounded:
                                deterministic_response_text = (
                                    f"हमारे यहां आधे घंटे के स्लॉट नहीं हैं, सिर्फ पूरे घंटे के। "
                                    f"क्या मैं {_format_date_hi(slot_date.isoformat())} को "
                                    f"{_format_time_hi(slot_time)} का अपॉइंटमेंट कन्फर्म कर दूं?"
                                )
                            else:
                                deterministic_response_text = (
                                    f"क्या मैं {_format_date_hi(slot_date.isoformat())} को "
                                    f"{_format_time_hi(slot_time)} का अपॉइंटमेंट कन्फर्म कर दूं?"
                                )
                        else:
                            # Requested slot taken -- not a confirmation claim,
                            # safe to let the LLM phrase a natural alternative
                            # using the full slot list below.
                            booking_reference_context = (
                                format_slots_for_reference(slot_date, slots)
                                + f". {slot_time} is already booked -- suggest an open slot."
                                + f" The customer's requested date, resolved from what they just "
                                f"said, is {slot_date.isoformat()} -- use this exact date, not "
                                f"any date mentioned earlier in the conversation."
                            )
                    else:
                        # Booking-ish turn but no specific time yet (e.g. just
                        # "book a service") -- let the LLM ask for a time,
                        # using the slot list as reference.
                        booking_reference_context = (
                            format_slots_for_reference(slot_date, slots)
                            + f" The customer's requested date, resolved from what they just "
                            f"said, is {slot_date.isoformat()} -- use this exact date when "
                            f"asking for a time, even if a different date was mentioned "
                            f"earlier in the conversation (the customer may have corrected "
                            f"themselves since)."
                        )

            if USE_DETERMINISTIC_BOOKING and deterministic_response_text is not None:
                # 🔥 Option B: skip intent classification, RAG, filler, and
                # the LLM call entirely for this turn -- speak the fixed
                # booking line built above and stop here.
                await self._speak_deterministic_turn(
                    deterministic_response_text, timing, transcript, turn_record,
                )
                return

            # ============================================================
            # STEP 1.5: Intent (fasttext, filler_service) + RAG — parallel
            # ------------------------------------------------------------
            # Dono ko turant fire karo, event loop ko block kiye bina
            # (fasttext predict aur chromadb query dono sync/blocking hain,
            # isliye executor thread mein bhejna zaroori hai). RAG ka result
            # LLM call se theek pehle await hoga -- tab tak filler audio
            # ban/play ho raha hoga, isliye RAG ki latency user ko dikhti
            # hi nahi (hidden behind filler window), aur agar RAG slow
            # nikla toh timeout se fail-open ho jaayega, LLM block nahi hoga.
            # ============================================================
            loop = asyncio.get_event_loop()

            if ENABLE_INTENT_AND_FILLER:
                _t0 = time.time()
                intent, confidence = await loop.run_in_executor(None, classify_intent, transcript)
                print(f"⏱️ [INTENT] classify_intent took {(time.time()-_t0)*1000:.0f}ms")

                if intent == "call_ending":
                    await self._end_call_turn(transcript, timing, turn_record)
                    return

                if intent == "callback":
                    await self._callback_turn(transcript, timing, turn_record)
                    return

                filler_text = filler_for_intent(intent, transcript=transcript)
                print(f"🗣️ [FILLER] '{filler_text}' (intent={intent}, confidence={confidence:.2f})")
                self._intent_history.append({"intent": intent, "confidence": confidence})
                self._filler_history.append(filler_text)
                self._customer_text_history.append(transcript) 

                rag_task = None
                if should_run_rag(intent):
                    rag_module = (self.session.get("cloud_context") or {}).get("module", "service")
                    rag_task = asyncio.create_task(
                        asyncio.to_thread(_rag_ask_sync, self.dealer, rag_module, self.branch, transcript, 3)
                    )
                else:
                    print(f"⏭️ [RAG] skipped for intent='{intent}'")
            else:
                intent, confidence = "generic", 1.0
                filler_text = None
                rag_task = None
                self._intent_history.append({"intent": intent, "confidence": confidence})
                self._filler_history.append(None)
                self._customer_text_history.append(transcript) 
                print("⏭️ [INTENT/FILLER] disabled via ENABLE_INTENT_AND_FILLER — skipping classification & filler")
                # rag_module = (self.session.get("cloud_context") or {}).get("module", "service")
                # rag_task = asyncio.create_task(
                #     asyncio.to_thread(_rag_ask_sync, self.dealer, rag_module, self.branch, transcript, 3)
                # )

            filler_cancelled = False   # 🔥 NEW: track karo taaki double-cancel na ho
            timing['filler_requested_at'] = time.time()

            self._turn_items = []
            self._turn_items_done = False
            pump_task = self._track(self._audio_pump(timing))

            filler_item = None
            if ENABLE_INTENT_AND_FILLER and filler_text:
                filler_item = await self._schedule_tts(filler_text, is_first_sentence=True, use_cache=True, is_filler=True)
            else:
                filler_cancelled = True

            rag_context = None
            RAG_SIMILARITY_MAX_DISTANCE = 0.45  # chroma cosine distance -- tune as needed

            if rag_task is not None:
                try:
                    rag_result = await asyncio.wait_for(rag_task, timeout=0.35)
                except asyncio.TimeoutError:
                    rag_result = None
                    print("⏱️ [RAG] timed out, proceeding without context")
                    # NOTE: timeout ko error log NAHI karte -- 350ms ka budget
                    # hai, iska time-out ho jaana normal design hai (fail-open).
                    # Sirf asli exception log hoti hai, neeche.
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    rag_result = None
                    print(f"❌ [RAG] ask_question failed: {e}")
                    logger.error(f"[RAG] ask_question failed (session={self.session_id}): {e}")
                    self._log_error("rag", "ask_question", e, severity="warning")   # 🔥 NEW

                if rag_result and rag_result.get("success"):
                    contexts = rag_result.get("contexts", [])
                    best_distance = rag_result.get("best_distance")

                    if contexts and best_distance is not None and best_distance <= RAG_SIMILARITY_MAX_DISTANCE:
                        rag_context = "\n".join(contexts)
                        print(f"📚 [RAG] using context (distance={best_distance:.3f}) for '{transcript[:40]}'")
                    else:
                        print(f"📚 [RAG] no close-enough match (distance={best_distance}), skipping context")

            print(f"🗣️ [FILLER] '{filler_text}' scheduled")

            # ============================================================
            # STEP 2: LLM (Streaming) — yeh waisa hi rahega, bas filler ke baad
            # ============================================================

            # 🔥 Reuse the customer_name/vehicle_model resolved once in
            # connect() instead of re-hardcoding "Customer"/"Unknown" every
            # turn -- keeps the LLM's context consistent with the greeting
            # the customer actually heard.
            base_context = self.session.get("cloud_context") or {
                "customer_name": "Customer",
                "vehicle_model": "Unknown",
                "due_date": "Unknown",
                "module": "general_query",
                "branch": "Unknown",
            }
            cloud_context = {
                **base_context,
                "rag_context": rag_context,
                "today": timezone.now().date().strftime("%Y-%m-%d (%A)"),
                "current_datetime_ist": _now_ist().strftime("%Y-%m-%d %H:%M"),
                "branch": base_context.get("branch", "Unknown"),
                "customer_history_summary": self._customer_text_history,
            }

            full_response = ""
            first_token_time = None
            llm_start_time = time.time()

            print(f"\n🤖 [LLM] Starting stream...")

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, True))

            text_buffer = ""
            tts_tasks = []
            first_sentence_sent = False
            # 🔥 Every _schedule_tts() call opens a brand-new Murf stream --
            # a cold restart of the voice model's prosody/pitch, not a
            # continuation. Flushing on every single short sentence (the old
            # behaviour) meant multi-sentence responses had an audible tone/
            # accent seam at every sentence boundary. Later sentences are
            # still batched together until there's enough text to be worth
            # a new TTS call, cutting the number of cold restarts in a
            # typical multi-sentence turn.
            # 🔥 LATENCY: the FIRST chunk no longer waits for a full
            # sentence-ending punctuation mark -- it flushes on a clause
            # boundary (comma/colon/semicolon/dash) once there's enough
            # text, or force-flushes at 3x that length even with no
            # punctuation at all, so a long or run-on first sentence
            # doesn't delay first audio.
            MIN_TTS_CHARS_FIRST = 25
            MIN_TTS_CHARS_AFTER_FIRST = 60

            async for chunk in self._llm_stream_async(
                self.session_id, transcript, cloud_context, filler_text=filler_text,
                # 🔥 Only RAG context (already intent-gated, distance-thresholded,
                # timeout-capped above) is passed through here. use_rag=False
                # because RAG was already decided/run above -- chat_turn_stream
                # must not redo it.
                reference_context=rag_context,
                use_rag=False,
                history=llm_history,
            ):
                # 🔥 First token timing
                if first_token_time is None:
                    first_token_time = time.time()
                    timing['llm_first_token_at'] = first_token_time
                    ttft = (first_token_time - llm_start_time) * 1000
                    print(f"⚡ [LLM] First token: {ttft:.0f}ms")

                full_response += chunk
                text_buffer += chunk
                if _LEAK_PATTERNS.search(text_buffer):
                    print(f"⚠️ [LLM] Leaked tool-call/JSON text detected, suppressing this turn's remaining TTS: {text_buffer!r}")
                    logger.warning("Leaked non-speech text from LLM | text=%r", text_buffer)
                    # 🔥 NEW: ye LLM ka misbehaviour hai -- crash nahi, par
                    # track hona chahiye (prompt/model regression ka signal).
                    self._log_error("llm", "tool_call_leak", "LLM leaked tool-call/JSON text",
                                    severity="warning", sample=text_buffer[:200])
                    text_buffer = ""
                    continue
                print(f"📝 [CHUNK] {chunk}", end="", flush=True)

                candidate = text_buffer.strip()
                if not candidate:
                    continue

                ends_sentence = chunk.endswith(('.', '?', '!', '।', '\n'))
                ends_clause = chunk.endswith((',', '—', ':', ';'))

                # 🔥 LATENCY: pehle sentence ke liye clause boundary (comma
                # etc.) ya ek forced-length cap par bhi flush karo -- poore
                # sentence-ending punctuation ka wait mat karo. Baad ke
                # sentences purani tarah hi sentence-end + min-length par
                # batch hote hain (naya Murf stream = cold prosody restart).
                if not first_sentence_sent:
                    should_flush = (
                        (ends_sentence or ends_clause) and len(candidate) >= MIN_TTS_CHARS_FIRST
                    ) or len(candidate) >= MIN_TTS_CHARS_FIRST * 3
                else:
                    should_flush = ends_sentence and len(candidate) >= MIN_TTS_CHARS_AFTER_FIRST

                if should_flush:
                    sentence = candidate
                    text_buffer = ""

                    # 🔥 FIX: real jawab ka pehla sentence ready hote hi filler ko turant CANCEL karo,
                    # warna filler aur real jawab ki audio dono ek sath overlay ho jaati hai (overlap bug)
                    filler_cancelled = True

                    print(f"\n🔥 [TTS] Chunk ready: {sentence[:50]}...")

                    await self._schedule_tts(sentence, is_first_sentence=not first_sentence_sent)
                    first_sentence_sent = True

            # Bacha hua text
            if text_buffer.strip():
                print(f"\n🔥 [TTS] Final buffer: {text_buffer[:50]}...")
                await self._schedule_tts(text_buffer.strip(), is_first_sentence=not first_sentence_sent)
                first_sentence_sent = True

            # 🔥 NEW: LLM ne kuch bola hi nahi -- provider down tha ya stream
            # khaali aayi. _llm_stream_async() ne exception already log kar
            # di hogi (agar exception tha); ye us case ke liye hai jahan
            # stream chup-chaap khaali aayi.
            if not full_response.strip():
                self._log_error("llm", "empty_response",
                                "LLM stream produced no text this turn",
                                severity="error", transcript=transcript[:200])

            turn_usage = get_last_turn_usage()
            turn_prompt_tokens = turn_usage.get("prompt_tokens") or 0
            turn_output_tokens = turn_usage.get("output_tokens") or 0
            self._session_prompt_tokens += turn_prompt_tokens
            self._session_output_tokens += turn_output_tokens

            if not filler_cancelled and len(self._turn_items) > 1:
                filler_item["cancel_event"].set()

            self._turn_items_done = True
            await pump_task
            # 🔥 Bot ka turn Redis history me save -- pehle ye chat_turn_stream()
            # ke andar hota tha, ab caller ki zimmedari hai.
            await database_sync_to_async(save_conversation)(
                self.session_id, "Aarohi", full_response
            )

            # 🔥🔥🔥 SAHI JAGAH: LLM complete hone ke BAAD, ek baar hi!
            turn_record["bot"] = full_response
            self.recording["transcript"].append(turn_record)
            print(f"📝 [TRANSCRIPT] Turn saved: User='{transcript[:30]}...' Bot='{full_response[:30]}...'")

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, False))

            # 🔥 LLM complete timing
            timing['llm_complete_at'] = time.time()
            llm_total = (timing['llm_complete_at'] - llm_start_time) * 1000
            print(f"\n⏱️ [LLM] Total LLM time: {llm_total:.0f}ms")

            # 🔥 User heard timing
            timing['user_heard_at'] = time.time()

            # 🔥 NEW: TTS cost -- `await pump_task` ke BAAD, isliye is turn ke
            # saare _schedule_tts() calls ho chuke hain aur count final hai.
            # _consume_tts_chars() greeting ke bache hue chars bhi jod deta
            # hai (pehle bot turn par) aur counter reset kar deta hai.
            turn_tts_cost = cost_tts(self._consume_tts_chars())

            # 🔥 DB: persist the bot's turn + full per-turn timing breakdown
            # NOTE: _track() khud create_task() karta hai -- yahan dobara
            # asyncio.create_task() lagane se Python 3.14 me TypeError aata
            # tha ("a coroutine was expected, got Task"), aur har bot turn
            # silently save hone se reh jaata tha.
            bot_turn_task = self._track(
                database_sync_to_async(save_turn)(
                    self.session_id, "bot", full_response,
                    timing=build_timing_record(timing),
                    intent=intent,
                    filler_text=filler_text or "",
                    tts_pricing=turn_tts_cost,      # 🔥 NEW
                )
            )

            # 🔥 per-turn accuracy/filler_accuracy/llm_pricing, scored and
            # persisted entirely in the background -- adds zero latency to
            # this turn's response. turn_prompt_tokens/turn_output_tokens
            # were captured synchronously above, before this point.
            self._score_tasks.append(
                _fire_and_forget(
                    self._score_and_persist_turn(
                        self.session_id, transcript, full_response, filler_text,
                        turn_prompt_tokens, turn_output_tokens, bot_turn_task,
                    ),
                    label="score_turn",
                )
            )

            # ============================================================
            # STEP 3: FULL TIMING REPORT
            # ============================================================
            print(f"\n{'='*60}")
            print(f"📊 [TIMING REPORT] Complete Turn Breakdown")
            print(f"{'='*60}")

            audio_to_stt = (timing['stt_done_at'] - timing['audio_received_at']) * 1000
            stt_to_llm_first = (timing['llm_first_token_at'] - timing['stt_done_at']) * 1000 if timing['llm_first_token_at'] else 0
            llm_first_to_complete = (timing['llm_complete_at'] - timing['llm_first_token_at']) * 1000 if timing['llm_first_token_at'] else 0
            llm_to_tts_first = timing['tts_first_audio_ms'] if timing['tts_first_audio_ms'] else 0
            tts_to_user = (timing['user_heard_at'] - timing['llm_complete_at']) * 1000

            total_turn = (timing['user_heard_at'] - timing['audio_received_at']) * 1000

            print(f"🎤 1. Audio Received → STT Done:        {audio_to_stt:>8.0f}ms")
            print(f"📝 2. STT Done → LLM First Token:       {stt_to_llm_first:>8.0f}ms")
            print(f"🤖 3. LLM First Token → LLM Complete:   {llm_first_to_complete:>8.0f}ms")
            print(f"🔊 4. LLM Complete → TTS First Audio:   {llm_to_tts_first:>8.0f}ms")
            print(f"📤 5. TTS First Audio → User Heard:     {tts_to_user:>8.0f}ms")
            print(f"{'-'*60}")
            print(f"⏱️  TOTAL TURN TIME (Audio → Heard):   {total_turn:>8.0f}ms")
            print(f"{'='*60}\n")
            if 'real_user_heard_at' in timing:
                real_perceived_latency = (timing['real_user_heard_at'] - timing['audio_received_at']) * 1000
                print(f"🎯 REAL latency (audio → pehli awaaz sunayi): {real_perceived_latency:.0f}ms")

            # Final text
            await self.safe_send(text_data=json.dumps({
                "type": "ai_response",
                "text": full_response,
                "back_flag": 2,
                "usage": "0",
            }))

            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "done"}))

        except asyncio.CancelledError:
            print("🚫 [WS] process_utterance cancelled (interrupt/disconnect)")
            if should_end_call(self.session_id):
                self._closed = True
                try:
                    await self.close(code=1000)
                except Exception as e:
                    print(f"⚠️ [END-CALL] close() after cancel raised: {e}")
            raise
        except Exception as e:
            # print(f"❌ [WS] Error: {e}")
            # 🔥 NEW: turn ka koi bhi unhandled crash -- ye "bot band ho gaya"
            # wala case hai. Yahan tak pahunchne ka matlab poora turn gir
            # gaya, isliye severity=critical.
            print(f"❌ [WS] process_utterance crashed: {e}")
            logger.exception(f"[WS] process_utterance crashed (session={self.session_id}): {e}")
            self._log_error("other", "process_utterance", e, severity="critical")
            # 🔥 FIX: pehle turn items cancel karo aur pump ko rukne do,
            # warna "done" ke baad bhi trailing audio bytes client ko mil sakte hain
            async with self._turn_items_lock:
                for item in self._turn_items:
                    item["cancel_event"].set()
            self._turn_items_done = True
            if 'pump_task' in dir() and pump_task and not pump_task.done():
                try:
                    await pump_task
                except Exception:
                    pass
            await self.safe_send(text_data=json.dumps({"type": "error", "message": str(e)}))
            await self.safe_send(text_data=json.dumps({"type": "done"}))

        finally:
            self.session["is_processing"] = False
            self._arm_bot_speaking_fallback(
                timing.get('tts_total_bytes', 0), self.BOT_AUDIO_SAMPLE_RATE, tag="turn-reply"
            )
            self._turn_items_done = True
            if self._current_process_task is asyncio.current_task():
                self._current_process_task = None

            end_call_signal = should_end_call(self.session_id)
            if end_call_signal:
                print(f"👋 [END-CALL] LLM called end_call "
                    f"(reason={end_call_signal.get('reason')}) -- closing socket")
                self._closed = True
                try:
                    await self.safe_send(text_data=json.dumps({"type": "call_ended"}))
                except Exception as e:
                    print(f"⚠️ [END-CALL] failed to send call_ended: {e}")
                try:
                    await self.close(code=1000)
                except Exception as e:
                    print(f"⚠️ [END-CALL] close() raised (socket likely already gone): {e}")

    async def do_stt_google(self, audio_bytes):
        """Google Speech-to-Text — STREAMING version"""
        try:
            from google.cloud import speech
            print("yes google stt is called")
            client = get_stt_client()
            if not client:
                return ""

            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                language_code="hi-IN",
                alternative_language_codes=["en-IN"],
                enable_automatic_punctuation=True,
                model="latest_short",
            )
            streaming_config = speech.StreamingRecognitionConfig(
                config=config,
                interim_results=False,   # sirf final result chahiye
            )

            # Audio ko chunks me todo (jaise real streaming me aata)
            CHUNK_SIZE = 3200  # ~100ms @16kHz mono int16
            chunks = [audio_bytes[i:i+CHUNK_SIZE] for i in range(0, len(audio_bytes), CHUNK_SIZE)]

            def request_generator():
                for chunk in chunks:
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)

            def run_streaming():
                responses = client.streaming_recognize(
                    config=streaming_config,
                    requests=request_generator(),
                )
                transcript = ""
                for response in responses:
                    for result in response.results:
                        if result.is_final and result.alternatives:
                            transcript += result.alternatives[0].transcript + " "
                return transcript.strip()

            loop = asyncio.get_event_loop()
            transcript = await loop.run_in_executor(None, run_streaming)
            return transcript

        except Exception as e:
            print(f"❌ [STT] Error: {e}")
            # 🔥 NEW: Google STT fallback bhi gira
            self._log_error("stt", "google_streaming_recognize", e, severity="error",
                            audio_bytes=len(audio_bytes) if audio_bytes else 0)
            return ""

    async def do_stt(self, audio_bytes):
        """60db STT — in-memory bytes, no disk write"""
        print("insnide 60db stt ")
        try:
            from .services.stt_service import transcribe_audio_bytes  # apna actual import path daalo
            print("🔵 [STT] Using 60db")
            print("insnide 60db stt core")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: transcribe_audio_bytes(audio_bytes, sample_rate=16000)
            )

            if result.get("_no_speech"):
                return ""

            return result.get("text", "").strip()

        except Exception as e:
            print(f"❌ [STT] Error: {e}")
            # 🔥 NEW: 60db batch path gira
            self._log_error("stt", "transcribe_audio_bytes", e, severity="error",
                            audio_bytes=len(audio_bytes) if audio_bytes else 0)
            return ""

    async def send_greeting(self):
        # 🔥 Dynamic greeting -- uses the customer_name/vehicle_model/branch
        # resolved in connect() (self.session["cloud_context"]). Falls
        # back to the original generic line when there's no phone_number
        # match (e.g. no ?phone= param, or the customer row was removed).
        context = self.session.get("cloud_context") or {}
        customer_name = context.get("customer_name", "Customer")
        vehicle_model = context.get("vehicle_model", "Unknown")
        branch_name = context.get("branch")
        if not branch_name or branch_name == "Unknown":
            branch_name = None

        dealer_intro = f"{branch_name}" if branch_name else "ओम होंडा"

        if customer_name and customer_name != "Customer":
            greeting = f"नमस्ते {customer_name} जी! मैं {dealer_intro} से आरोही बोल रही हूँ।"
            if vehicle_model and vehicle_model != "Unknown":
                greeting += f" आपकी {vehicle_model} की सर्विस के बारे में बात करनी थी।"
        else:
            greeting = f"नमस्ते जी! मैं {dealer_intro} से आरोही बोल रही हूँ।"

        if self._closed:
            return

        # 🔥 FIX: persist the greeting into conversation history BEFORE it's
        # spoken, and BEFORE any turn processing can start. Previously this
        # was never saved at all -- get_conversation_history() came back
        # empty for turn 1, so the LLM had no idea it had already
        # introduced itself and stated the reason for the call, and would
        # re-introduce itself / restate the purpose again on the very next
        # turn (the repetitive "main Om Honda se call kar rahi hoon..."
        # behaviour). Awaited (not fire-and-forget) so there's no race with
        # the first customer turn reading history back out.
        await database_sync_to_async(save_conversation)(self.session_id, "Aarohi", greeting)

        await self.safe_send(text_data=json.dumps({
            "type": "transcript",
            "text": greeting
        }))

        tts = get_tts_service()
        try:
            audio_stream = await database_sync_to_async(tts.synthesize_stream)(greeting)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 🔥 NEW: greeting hi TTS se nahi bani -- call practically dead
            # hai (customer ko kuch sunai nahi dega), isliye critical.
            print(f"❌ [TTS] greeting synthesis failed: {e}")
            logger.error(f"[TTS] greeting synthesis failed (session={self.session_id}): {e}")
            self._log_error("tts", "greeting_synthesize_stream", e, severity="critical",
                            greeting_chars=len(greeting))
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "done"}))
            return

        # 🔥 NEW: GREETING KA TTS COST.
        # Greeting yahan bolti hai, par uska koi ConversationTurn row nahi
        # banta -- CallSession abhi bani hi nahi (_ensure_call_session()
        # pehle client message par chalta hai). Isliye chars park kar do;
        # _consume_tts_chars() inhe PEHLE bot turn ke saath DB me daal
        # dega. Warna har call me ~100 char ka TTS chupchaap gum ho jaata.
        # += isliye (= nahi) kyunki "init" message par send_greeting()
        # dobara chal sakta hai aur pehla greeting abhi bhi unbilled ho.
        self._greeting_tts_chars += len(greeting)

        async with self._tts_send_lock:
            if self._closed:
                return

            self.session["bot_speaking"] = True   # 🔥 FIX: greeting bhi "bot speaking" state hai
            self._turn_start_time = time.time()   # 🔥 FIX 1
            self._reset_playout_state()  # 🔥 NEW: fresh pacing clock for this greeting
            await self.safe_send(text_data=json.dumps({"type": "pcm_start"}))

            total_bytes_sent = 0
            try:
                buffer = bytearray()
                chunk_count = 0

                for chunk in audio_stream:
                    if self._closed:
                        break
                    if chunk:
                        buffer.extend(chunk)
                        total_bytes_sent += len(chunk)
                        chunk_count += 1

                        if chunk_count >= 10 or len(buffer) >= 8192:
                            # 🔥 UPDATED: paced via _send_bot_pcm (writes the
                            # recording positionally itself) instead of a raw
                            # _write_positional() + safe_send() pair sent as
                            # fast as chunks arrived.
                            await self._send_bot_pcm(bytes(buffer))
                            buffer = bytearray()
                            chunk_count = 0

                if len(buffer) > 0 and not self._closed:
                    await self._send_bot_pcm(bytes(buffer))

                await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
                await self.safe_send(text_data=json.dumps({"type": "done"}))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 🔥 NEW: stream ke beech me Murf gir gaya (generator ke andar
                # exception). Greeting adhoori reh gayi.
                print(f"❌ [TTS] greeting stream broke mid-playback: {e}")
                logger.error(f"[TTS] greeting stream broke (session={self.session_id}): {e}")
                self._log_error("tts", "greeting_stream", e, severity="error",
                                bytes_sent=total_bytes_sent)
            finally:
                # 🔥 FIX (was: clear bot_speaking instantly here): the server
                # finishing this send loop does NOT mean the client has
                # finished PLAYING the greeting -- that takes real seconds
                # longer. Arm a fallback based on the actual remaining
                # playout time (now that sends are real-time paced, see
                # _arm_bot_speaking_fallback); playback_end (sent by the
                # client when it's truly done) is still the primary/
                # preferred signal and cancels this fallback when it
                # arrives on time. See _clear_bot_speaking_after_delay.
                self._arm_bot_speaking_fallback(
                    total_bytes_sent, self.BOT_AUDIO_SAMPLE_RATE, tag="greeting"
                )

    def _build_recording_basename(self):
        """Human-readable recording filename base, e.g. 'OMH_SALES_1000',
        instead of the raw session UUID -- makes recordings easy to find by
        dealer/module/customer instead of only by session_id. Falls back to
        session_id if none of the pieces are available."""
        def _clean(value):
            if not value:
                return ""
            value = str(value).strip().upper()
            value = re.sub(r'[^A-Z0-9]+', '_', value)
            return value.strip('_')

        dealer_code = _clean(
            getattr(self.dealer, 'code', None) or getattr(self.dealer, 'name', None)
        )
        context = self.session.get("cloud_context") or {}
        module_code = _clean(context.get("module"))
        customer_code = _clean(self.customer_id) or _clean(self.phone_number)

        parts = [p for p in (dealer_code, module_code, customer_code) if p]
        if not parts:
            parts = ["CALL", self.session_id[:8]]

        return "_".join(parts)

    async def save_conversation_recording(self):
        """
        🔥 Ab user aur bot alag-alag channels mein hain (koi additive clipping nahi).
        Do files banate hain:
        1. _stereo.mp3  → Left=User, Right=Bot (sabse "proper" — dono clearly separate)
        2. _mixed.mp3   → single mono file, safe averaging (add+clip nahi, halka downmix)
        """
        print(f"💾 [RECORD] Saving conversation for session {self.session_id}")

        user_bytes = bytes(self.recording["user_audio"])
        bot_bytes = bytes(self.recording["bot_audio"])

        if max(len(user_bytes), len(bot_bytes)) < 1000:
            print(f"⚠️ [RECORD] Too short, skipping")
            return

        temp_dir = settings.CALL_RECORDINGS_DIR
        os.makedirs(temp_dir, exist_ok=True)

        timestamp = timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S")
        filename = f"{self._build_recording_basename()}_{timestamp}"

        # --- pad dono ko same length tak (odd-byte safe) ---
        if len(user_bytes) % 2:
            user_bytes += b'\x00'
        if len(bot_bytes) % 2:
            bot_bytes += b'\x00'
        max_len = max(len(user_bytes), len(bot_bytes))
        user_bytes = user_bytes.ljust(max_len, b'\x00')
        bot_bytes = bot_bytes.ljust(max_len, b'\x00')

        left = np.frombuffer(user_bytes, dtype=np.int16)
        right = np.frombuffer(bot_bytes, dtype=np.int16)
        n = min(len(left), len(right))
        left, right = left[:n], right[:n]

        # --- 1) STEREO WAV: L=user, R=bot — dono clearly separate, proper recording ---
        stereo = np.empty(n * 2, dtype=np.int16)
        stereo[0::2] = left
        stereo[1::2] = right

        stereo_wav = os.path.join(temp_dir, f"{filename}_stereo_temp.wav")
        with wave.open(stereo_wav, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.RECORD_SAMPLE_RATE)
            wf.writeframes(stereo.tobytes())

        stereo_seg = AudioSegment.from_wav(stereo_wav)
        stereo_mp3 = os.path.join(temp_dir, f"{filename}_stereo.mp3")
        stereo_seg.export(stereo_mp3, format="mp3", bitrate="96k")
        print(f"📱 [RECORD] Stereo saved (L=user, R=bot): {stereo_mp3}")

        # --- 2) MONO downmix — safe averaging, koi clipping nahi ---
        mono = ((left.astype(np.int32) + right.astype(np.int32)) // 2).astype(np.int16)

        mono_wav = os.path.join(temp_dir, f"{filename}_mixed_temp.wav")
        with wave.open(mono_wav, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.RECORD_SAMPLE_RATE)
            wf.writeframes(mono.tobytes())

        mono_seg = AudioSegment.from_wav(mono_wav)
        mixed_mp3 = os.path.join(temp_dir, f"{filename}_mixed.mp3")
        mono_seg.export(mixed_mp3, format="mp3", bitrate="64k")
        print(f"📱 [RECORD] Mono downmix saved: {mixed_mp3}")

        duration_seconds = n / self.RECORD_SAMPLE_RATE

        try:
            updated = await database_sync_to_async(_persist_recording_paths_sync)(
                self.session_id, stereo_mp3, mixed_mp3, duration_seconds,
            )
            if updated:
                print(f"💾 [RECORD] CallSession updated with recording paths + "
                    f"duration={duration_seconds:.1f}s")
            else:
                print(f"⚠️ [RECORD] No CallSession row found for session_id={self.session_id} "
                    f"-- recording paths were NOT saved to DB")
        except Exception as e:
            print(f"❌ [RECORD] Failed to persist recording paths to DB: {e}")
            # 🔥 NEW
            self._log_error("db", "_persist_recording_paths_sync", e, severity="error",
                            duration_seconds=round(duration_seconds, 1))

        # Metadata
        metadata = {
            "session_id": self.session_id,
            "start_time": self.recording["start_time"],
            "end_time": time.time(),
            "duration_seconds": duration_seconds,
            "transcript": self.recording["transcript"],
            "files": {
                "stereo": stereo_mp3,   # 🔥 proper — L=user, R=bot
                "mixed": mixed_mp3,     # single-track quick listen
            }
        }

        meta_path = os.path.join(temp_dir, f"{filename}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        # Cleanup temp
        for p in (stereo_wav, mono_wav):
            if os.path.exists(p):
                os.remove(p)

        return stereo_mp3

    def _bytes_to_wav(self, audio_bytes: bytes, output_path: str, sample_rate: int = 16000):
        """Raw PCM bytes ko WAV file mein convert karo"""

        # 🔥 FIX 1: Pad karo agar odd bytes hain
        if len(audio_bytes) % 2 != 0:
            audio_bytes = audio_bytes + b'\x00'  # Zero byte add karo

        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)