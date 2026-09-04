import json
import base64
import asyncio
import time
import uuid
import random
import logging
import os
import wave
import threading
from collections import deque
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import numpy as np
from django.conf import settings
from django.utils import timezone
from pydub import AudioSegment
from .services.cloud_llm_service import chat_turn_stream
from .services.tts_service import get_tts_service
from .services.stt_service import STTSession
from .services.conversation_history import (
    init_state, set_speech_state, set_final_transcript, set_generating, clear_state,
    save_conversation,
)
# 🔥 SAB DB kaam ab views.py se
# 🔥 NEW: log_service_error -- provider failures ka DB row (ServiceErrorLog).
from .views import (
    get_or_create_call_session, save_turn, end_call_session,
    set_dialer_call_id, get_customer_context, finalize_call_summary,
    get_history_for_llm, log_service_error,
)
# build_timing_record / chat_turn_stream me DB nahi -- service me hi hain
from .services.cloud_llm_service import chat_turn_stream, build_timing_record

from decimal import Decimal, ROUND_HALF_UP      # 🔥 NEW: cost math

from .views import (
    extract_slot_request, mentions_confirmation, get_available_slots,
    format_slots_for_reference, book_slot_for_session,
)
from .services.filler_service import pick_filler_detailed

# 🔥 FILLER CACHE: same pre-generated PCM cache consumers.py uses. Cached
# blobs are 24kHz Murf PCM, so they go through the same incremental
# resample path as live synthesis -- nothing special needed downstream.
try:
    from .services.filler_audio_cache import load_cached_pcm
except Exception:  # pragma: no cover
    load_cached_pcm = None

# 🔥 RECORDING: reuses the same persisting helper consumers.py uses
# (views_admin.py) so recordings from either transport land the same way.
try:
    from .views_admin import _persist_recording_paths_sync, _resolve_dealer_branch_sync
except Exception:  # pragma: no cover
    _persist_recording_paths_sync = None

import io
import audioop
import re
from .tools.tool_registry import (
    should_end_call, register_end_call_handler, unregister_end_call_handler,
    set_call_context, clear_call_context,
)
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

def _now_ist():
    return timezone.now().astimezone(IST)

logger = logging.getLogger('voice_bot')

# ──────────────────────────────────────────────────────────────
# Google STT lazy init
# ──────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────
# Plivo Constants
# ──────────────────────────────────────────────────────────────
PLIVO_CHUNK_BYTES = 1600      # 320-byte multiple
ECHO_GRACE_S = 0.15 
INTERRUPT_THRESHOLD = 600            # SPEECH_THRESHOLD * 1.5
INTERRUPT_MIN_MS = 250
PRE_ROLL_MS = 200
MIN_SPEECH_BYTES = 8000
MAX_BUFFER_SECONDS = 10
BYTES_PER_SEC = 16000                # 8kHz s16le = 16000 bytes/sec
PLIVO_SAMPLE_RATE = 8000
SPEECH_THRESHOLD = 400
SPEECH_MIN_MS = 300
MAX_BUFFER = BYTES_PER_SEC * MAX_BUFFER_SECONDS
SILENCE_END_MS = 700          # था 800 -- har turn me seedha 300ms bachega
PLAYOUT_LEAD_S = 1.0          # Plivo ke buffer me itne second se zyada aage kabhi mat bhejo
# Murf ka native output rate (tts_service.synthesize_stream sample_rate=24000)
MURF_SAMPLE_RATE = 24000
TTS_PREBUFFER_MS = 300
TTS_PREBUFFER_BYTES = int(BYTES_PER_SEC * TTS_PREBUFFER_MS / 1000)


# ═══════════════════════════════════════════════════════════════
# 🔥 NEW: COST CALCULATION (is file ka apna -- koi cross-import nahi)
# ---------------------------------------------------------------
# Ye file production dialer path hai; consumers.py sirf React test
# client ke liye hai. Dono ek doosre par depend NA karein, isliye
# rates/helpers yahan apne hain -- rate badle to DONO jagah badalna.
#
# Yahan ka mic audio 8kHz hai (PLIVO_SAMPLE_RATE), 16kHz nahi --
# cost_stt_from_bytes ka default isi hisaab se PLIVO_SAMPLE_RATE hai.
#
# LLM ka cost YAHAN NAHI -- wo score_and_price_turn() (per turn) aur
# generate_call_summary() (per call) se aata hai, unhe chhua nahi.
# Dialer ka apna cost bhi yahan nahi -- wo CallSession par
# views.recalc_call_cost() lagata hai, end_call_session() ke andar se.
#
# Decimal isliye ki ConversationTurn.stt_pricing/tts_pricing
# DecimalField(decimal_places=6) hain -- float me rounding turn-dar-turn
# jamaa hoke galat total banata hai.
# ═══════════════════════════════════════════════════════════════
STT_PER_HOUR = Decimal('27')        # Gnani WebSocket STT: Rs 27/hour

USD_TO_INR       = Decimal('88')    # Murf USD me quote karta hai
TTS_USD_PER_1K   = Decimal('0.01')  # Murf Falcon: $0.01 / 1000 chars
TTS_PER_1K_CHARS = TTS_USD_PER_1K * USD_TO_INR      # about Rs 0.88

_COST_Q = Decimal('0.000001')       # 6 decimal places -- field ke barabar


def _quantize_cost(value):
    return Decimal(value).quantize(_COST_Q, rounding=ROUND_HALF_UP)


def cost_stt_from_bytes(audio_bytes_len, sample_rate=PLIVO_SAMPLE_RATE):
    """PCM16 mono bytes ki length se STT cost.

    bytes / (sample_rate * 2) = seconds  (2 bytes per sample)
    Default 8kHz -- Plivo ka mic stream isi rate par aata hai.
    """
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


# 🔥 BARGE-IN: pitch + loudness confirmation (mirrors consumers.py's
# _handle_barge_in_audio).
INTERRUPT_PITCH_MIN_HZ = 100
INTERRUPT_PITCH_MAX_HZ = 300
INTERRUPT_PITCH_PERIODICITY_MIN = 0.30
INTERRUPT_VOICE_RATIO_THRESHOLD = 0.40
INTERRUPT_DIP_GRACE_S = 0.25   # single below-threshold chunk doesn't reset progress

# 🔥 LATENCY: pehla sentence poora hone ka wait mat karo -- itne chars
# jamte hi TTS fire kar do. Baad ke sentences batch hote hain taaki har
# sentence par naya Murf stream (cold prosody restart) na khule.
MIN_TTS_CHARS_FIRST = 40
MIN_TTS_CHARS_AFTER_FIRST = 80
SILENCE_CHECKIN_S = 8.0      # itni der listening state me kuch na bole to check-in
SILENCE_DISCONNECT_S = 12.0  # check-in ke BAAD itni der aur chup rahe to call kaato
SILENCE_WATCHDOG_TICK_S = 1.0

SILENCE_CHECKIN_TEXTS = [
    "क्या हुआ सर, आप हैं कि नहीं? आवाज़ नहीं आ रही।",
    "हेलो सर, क्या आप होल्ड पर हैं? मुझे कुछ सुनाई नहीं दे रहा।",
]
SILENCE_GOODBYE_TEXT = (
    "ठीक है सर, लगता है अभी बात करना मुश्किल है। मैं थोड़ी देर बाद "
    "फिर से कॉल करती हूँ। धन्यवाद, नमस्ते।"
)

# Speech-adaptation phrases for STT (Google fallback path only).
STT_PHRASES = [
    "Om Honda", "एड्रेस", "पता", "शोरूम",
    "Activa", "एक्टिवा", "Shine", "शाइन",
    "Unicorn", "यूनिकॉर्न", "Dio", "डियो",
    "SP125", "CB350", "टेस्ट राइड",
    "इंश्योरेंस", "सर्विसिंग", "EMI", "डाउन पेमेंट",
    "462001", "भोपाल",
]


# ──────────────────────────────────────────────────────────────
# Cut-call keywords
# ──────────────────────────────────────────────────────────────
CUT_CALL_KEYWORDS = [
    "kaat", "kaat do", "phone rakh", "baad mein", "cut",
    "रखो", "काटो",
    "फोन काट", "काट दो", "रख दो", "फोन काट दो",
]

# ──────────────────────────────────────────────────────────────
# Reprompt texts
# ──────────────────────────────────────────────────────────────
REPROMPT_TEXTS = [
    "जी, कृपया फिर से बोलिए...",
    "मुझे सुनाई नहीं दिया, क्या कह रहे थे?",
    "जी, आवाज़ कम आ रही है, फिर से बोलिए...",
]

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

def _is_mid_number_colon(text: str) -> bool:
    """True if `text` ends with ':' immediately preceded by a digit --
    i.e. it's a time value like '4:' from '4:00', not a real clause
    boundary. Flushing here splits the number across two independent
    Murf streams, causing an audible gap + splice noise mid-word."""
    if not text.endswith(':'):
        return False
    stripped = text[:-1].rstrip()
    return bool(stripped) and stripped[-1].isdigit()

# ═══════════════════════════════════════════════════════════════
# PLIVO DIALER CONSUMER
# ═══════════════════════════════════════════════════════════════
class PlivoDialerConsumer(AsyncWebsocketConsumer):
    """
    WebSocket: Plivo → Gnani/Sarvam/Google STT → Cloud LLM (Aarohi persona)
    → Murf TTS Streaming.

    🔥 v2 changes vs the original:
      1. Turn ab BACKGROUND task me chalta hai (_run_turn), receive() ke
         andar await nahi -- warna Channels ke sequential message handling
         ki wajah se turn ke dauraan aane wale media frames queue me atke
         rehte the aur barge-in detect hi nahi hota tha.
      2. Barge-in ab is_processing ke dauraan bhi chalta hai (sirf
         bot_speaking me nahi) -- filler/generation window bhi covered.
      3. REAL streaming TTS: chunk aate hi incremental audioop.ratecv se
         resample karke Plivo ko bheja jaata hai, poora sentence buffer
         karke nahi (yeh hi 450-1100ms wali TTS latency thi).
      4. Single ordered _audio_pump -- sentences kabhi interleave nahi
         hote, aur cancel karna ek hi jagah se hota hai.

    🔥 v3 (cost + error tracking):
      5. Per-turn STT/TTS cost ConversationTurn par save hota hai
         (stt_pricing / tts_pricing). Dialer ka apna cost CallSession
         par recalc_call_cost() se aata hai -- wo end_call_session()
         ke andar khud chalta hai, yahan kuch karne ki zaroorat nahi.
      6. Har external provider ki failure ServiceErrorLog me jaati hai
         (_log_error) -- STT/TTS/LLM/RAG/booking/recording, sab.
    """
    def _on_end_call_signal(self, payload):
        """Fires the instant end_call's tool impl runs (mid-LLM-stream), well
        before this turn's closing line has started TTS. Mirrors consumers.py's
        _on_end_call_signal -- lets _process_plivo_audio's interrupt-detection
        branch refuse to fire for the rest of this turn, so a barge-in can't
        cut off the goodbye line."""
        self._call_ending = True
        print(f"🔒 [END-CALL] signalled early (reason={payload.get('reason')}) -- "
            f"this turn is now barge-in-immune")

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
                    context={**(context or {}), 'transport': 'plivo'},
                ),
                label=f"errlog:{provider}/{stage}",
            )
        except Exception as e:
            logger.warning(f"[ERRLOG] could not schedule error log ({provider}/{stage}): {e}")

    def _consume_tts_chars(self):
        """🔥 NEW: is turn ke TTS characters lo aur counters reset kar do.

        _pending_tts_chars me greeting/reprompt jaise turn-ke-BAHAR bole
        gaye lines ke chars jama rehte hain -- unka apna koi
        ConversationTurn row nahi banta, isliye unka paisa AGLE bot turn
        ke saath DB me jaata hai. Ek hi baar -- yahin dono 0 ho jaate hain.
        """
        total = self._turn_tts_chars + self._pending_tts_chars
        if self._pending_tts_chars:
            print(f"💰 [COST] greeting/reprompt ke {self._pending_tts_chars} TTS "
                  f"chars is turn me jod diye")
        self._turn_tts_chars = 0
        self._pending_tts_chars = 0
        return total

    async def _warm_llm_connection(self):
        from .services.cloud_llm_service import warmup_module
        module = self.session["cloud_context"].get("module", "general_query")
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, warmup_module, module)
        except Exception as e:
            print(f"⚠️ [WARMUP] executor failed: {e}")
            self._log_error("llm", "warmup", e, severity="warning", module=module)

    async def connect(self):
        await self.accept()

        url_kwargs = self.scope.get("url_route", {}).get("kwargs", {}) or {}
        self.session_id = url_kwargs.get("session_id") or str(uuid.uuid4())
        self.phone_number = url_kwargs.get("phone") or None
        self.client_type = "plivo"
        self._customer_text_history = []

        self.session = {
            "history": [],
            "is_processing": False,
            "bot_speaking": False,
            "bot_speaking_until": 0.0,
            "stream_sid": None,
            "last_answer_norm": "",
            "empty_count": 0,
            "reprompt_idx": 0,
            "last_transcript": "",
            "interrupt_count": 0,
            "avg_rag_ms": 5000.0,
            "pending_slot": None,
            "has_conversation": False,
            "cloud_context": {
                "customer_name": "Customer",
                "vehicle_model": "Unknown",
                "due_date": "Unknown",
                "module": "general_query",
            },
        }

        self.dealer = None
        self.branch = None
        if _resolve_dealer_branch_sync is not None:
            try:
                self.dealer, self.branch = await database_sync_to_async(_resolve_dealer_branch_sync)(
                    self.phone_number, None
                )
            except Exception as e:
                print(f"❌ [DB] _resolve_dealer_branch_sync failed: {e}")
                logger.error(f"[DB] _resolve_dealer_branch_sync failed (session={self.session_id}): {e}")
                self._log_error("db", "_resolve_dealer_branch_sync", e, severity="warning",
                                phone=self.phone_number)

        self.session["cloud_context"]["branch"] = getattr(self.branch, "name", None) or "Unknown"
        self.session["cloud_context"]["dealer"] = self.dealer 
        self.session["cloud_context"]["branch_obj"] = self.branch

        # VAD state
        self._audio_buffer = bytearray()
        self._pre_roll = deque()
        self._pre_roll_ms = 0.0
        self._speech_started = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._interrupt_ms = 0.0
        self._ignore_until = 0.0
        self._last_voice_time = None
        self._stt_session = None

        # 🔥 SILENCE WATCHDOG state
        self._listening_since = None     # kab se hum "listening" state me idle hain
        self._silence_stage = 0          # 0=normal, 1=checkin bol chuke, 2=disconnect ho chuka
        self._silence_watchdog_task = None

        # 🔥 BARGE-IN state (pitch+RMS confirmation, grace period)
        self._interrupt_frame_count = 0
        self._interrupt_voiced_count = 0
        self._interrupt_last_loud_time = None
        self._interrupt_seed = bytearray()   # 🔥 barge-in ke dauraan jama hui user awaaz

        # 🔥 Gnani ki server-side VAD ne is turn ka speech-end detect kar
        # liya. Recv-loop thread se set hota hai, agle media frame par
        # padha jaata hai (dekho _process_plivo_audio ka PROCESS CHECK).
        self._gnani_speech_end = False
        self._last_checkpoint_name = None
        self._playout_end = 0.0

        self._closed = False
        self._active_tasks = set()
        self._stt_connect_task = None

        self._current_turn_partial_text = ""
        self._interrupt_pending = None
        self._spoken_texts_this_turn = []

        # 🔥 ORDERED TTS PUMP state (mirrors consumers.py's _turn_items)
        self._turn_items = []
        self._turn_items_lock = asyncio.Lock()
        self._turn_items_done = False

        # 🔥 NEW: TTS COST TRACKING
        # _turn_tts_chars   : is turn me Murf ko bheje gaye characters.
        #                     Cached filler ise nahi badhata (uska paisa 0).
        # _pending_tts_chars: greeting/reprompt ke chars -- inka apna
        #                     ConversationTurn row nahi banta, isliye
        #                     agle bot turn ke saath DB me jaate hain.
        self._turn_tts_chars = 0
        self._pending_tts_chars = 0

        # ═══════════════════════════════════════════════════════
        # 🔥 CALL RECORDING (mirrors consumers.py) -- separate user/bot
        # channels written at their real elapsed-time position.
        # ═══════════════════════════════════════════════════════
        self.RECORD_SAMPLE_RATE = PLIVO_SAMPLE_RATE
        self._write_cursor = {"user": 0, "bot": 0}
        self.recording = {
            "active": False,
            "user_audio": bytearray(),
            "bot_audio": bytearray(),
            "start_time": None,
            "transcript": [],
        }
        self._audio_lock = asyncio.Lock()
        self._current_turn_record = None
        self._call_ending = False
        self._greeting_active = False   # 🔥 NEW
        self._greeting_done = False 
        register_end_call_handler(self.session_id, asyncio.get_event_loop(), self._on_end_call_signal)

        asyncio.create_task(database_sync_to_async(init_state)(self.session_id))
        asyncio.create_task(
            database_sync_to_async(get_or_create_call_session)(
                self.session_id, phone_number=self.phone_number
            )
        )

        print(f"🔌 [Plivo] Client connected, session_id={self.session_id}")

        self._stt_session = STTSession(sample_rate=PLIVO_SAMPLE_RATE, phrases=STT_PHRASES)
        # 🔥 Gnani ka speech-end signal seedha hamare VAD ko de do -- isi par
        # turn dispatch hoga, apna SILENCE_END_MS ka wait poora kiye bina.
        self._stt_session.on_speech_end = self._on_gnani_speech_end
        # NOTE: deliberately NOT _track()'d -- _cancel_current_turn() would
        # otherwise kill the STT handshake on the first barge-in.
        self._stt_connect_task = asyncio.create_task(self._stt_session.connect())

        await self._refresh_customer_context()
        self._track(self._warm_llm_connection())
        self._silence_watchdog_task = asyncio.create_task(self._silence_watchdog())

    def _on_gnani_speech_end(self):
        """🔥 Gnani ki server-side VAD ne end-of-speech detect kiya.

        Standalone test me ye signal ("⏳ speech-end detected") hamare local
        RMS VAD se pehle aata hai, aur uske ~200ms baad transcript aa jaata
        hai. Hamara apna VAD energy-based hai aur uske upar 700ms silence
        ginta hai -- yaani har turn me seedha 400-700ms bekaar jaate the.

        Ye ek SYNC callback hai (STT recv-loop se call hota hai), isliye
        yahan sirf flag set karte hain. Asli dispatch agle media frame par
        _process_plivo_audio me hota hai, jahan poora VAD state (buffer,
        speech_started, stt_session) pehle se haath me hai.
        """
        if self._speech_started:
            self._gnani_speech_end = True
            
    async def _refresh_customer_context(self):
        try:
            ctx = await database_sync_to_async(get_customer_context)(self.phone_number)
        except Exception as e:
            print(f"❌ [DB] get_customer_context failed: {e}")
            logger.error(f"[DB] get_customer_context failed (session={self.session_id}): {e}")
            self._log_error("db", "get_customer_context", e, severity="error",
                            phone=self.phone_number)
            return
        self.session["cloud_context"]["customer_name"] = ctx["customer_name"]
        self.session["cloud_context"]["vehicle_model"] = ctx["vehicle_model"]
        self.session["cloud_context"]["due_date"] = ctx.get("due_date", "Unknown")
        self.session["cloud_context"]["module"] = ctx.get("module", "general_query")
        print(f"🔎 [CONTEXT] module resolved to: {self.session['cloud_context']['module']!r} for phone {self.phone_number}")
        set_call_context(self.session_id, phone_number=self.phone_number, customer_name=ctx["customer_name"])

    async def disconnect(self, close_code):
        print(f"🔌 [WS] Disconnected: {close_code}")

        self._closed = True
        unregister_end_call_handler(self.session_id)
        clear_call_context(self.session_id)

        if self.session.get("has_conversation") and self.recording.get("active"):
            try:
                await self.save_conversation_recording()
            except Exception as e:
                print(f"❌ [RECORD] Failed to save: {e}")
                # 🔥 NEW
                self._log_error("recording", "save_conversation_recording", e,
                                severity="error", close_code=close_code)

        async with self._turn_items_lock:
            for item in self._turn_items:
                item["cancel_event"].set()

        pending = [t for t in self._active_tasks if not t.done()]
        print(f"🔌 [WS] Disconnected: {close_code}, cancelling {len(pending)} pending task(s)")
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if self._stt_connect_task and not self._stt_connect_task.done():
            self._stt_connect_task.cancel()

        if self._silence_watchdog_task and not self._silence_watchdog_task.done():
            self._silence_watchdog_task.cancel()

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
        # ka pulse cost (CallSession.dialer_pricing) wahan compute hota
        # hai. Yahan alag se kuch karne ki zaroorat nahi.
        _fire_and_forget(
            database_sync_to_async(end_call_session)(self.session_id, status),
            label="end_call_session",
        )
        if self.session.get("has_conversation"):
            _fire_and_forget(
                database_sync_to_async(finalize_call_summary)(
                    self.session_id, self.session.get("cloud_context")
                ),
                label="finalize_call_summary",
            )

    def _track(self, coro):
        """Wrap asyncio.create_task so the task is cancellable on disconnect."""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _cancel_current_turn(self):
        """Cancel the in-flight turn AND stop every pending TTS synthesis
        thread, then WAIT for everything to actually stop."""
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

    async def _flush_playback_buffer(self):
        """Stop audio already handed off to Plivo, on barge-in."""
        if self.session.get("stream_sid"):
            await self.safe_send(text_data=json.dumps({
                "event": "clearAudio",
                "streamId": self.session["stream_sid"],
            }))

    async def safe_send(self, text_data=None, bytes_data=None):
        if self._closed:
            return
        try:
            await self.send(text_data=text_data, bytes_data=bytes_data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._closed = True
            print(f"⚠️ [Plivo] send failed (caller likely gone): {e}")

    # ═══════════════════════════════════════════════════════════
    # RECEIVE — Route Plivo events
    # ═══════════════════════════════════════════════════════════
    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            data = json.loads(text_data)
            if "event" in data:
                await self._handle_plivo_event(data)
            else:
                print(f"⚠️ [Plivo] Unknown message: {data}")
        elif bytes_data:
            print("⚠️ [Plivo] Unexpected binary data received")

    # ═══════════════════════════════════════════════════════════
    # PLIVO EVENT HANDLER
    # ═══════════════════════════════════════════════════════════
    async def _handle_plivo_event(self, data):
        event = data.get("event")

        if event == "start":
            self.session["stream_sid"] = (
                data.get("start", {}).get("streamId")
                or data.get("streamId")
            )
            print(f"📞 [Plivo] Call started: {self.session['stream_sid']}")
            asyncio.create_task(
                database_sync_to_async(set_dialer_call_id)(self.session_id, self.session["stream_sid"])
            )

            self.recording["active"] = True
            self.recording["start_time"] = time.time()
            print(f"🔴 [RECORD] Started recording for session {self.session_id}")

            # 🔥 greeting bhi background me -- warna greeting bajne tak
            # incoming media frames block rehte hain.
            self._track(self._send_greeting())

        elif event == "stop":
            print("🔌 [Plivo] Call ended")
            asyncio.create_task(database_sync_to_async(end_call_session)(self.session_id, "completed"))
            asyncio.create_task(
                database_sync_to_async(finalize_call_summary)(
                    self.session_id, self.session.get("cloud_context")
                )
            )
            await self.close()

        elif event == "playedStream":
            # 🔥 FIX: pehle HAR checkpoint ack par bot_speaking=False ho
            # jaata tha. Filler ka ack aata tha jabki asli jawab abhi baj
            # raha hota tha -> barge-in gate band -> bot rukti hi nahi thi.
            # Ab sirf turn ke AAKHIRI checkpoint ka ack maana jaata hai,
            # aur wo bhi tabhi jab playout window sach me khatam ho chuki ho.
            name = data.get("name") or data.get("playedStream", {}).get("name")
            if self._last_checkpoint_name and name and name != self._last_checkpoint_name:
                print(f"⏭️ [Plivo] stale checkpoint ack ({name}) — ignoring")
                return
            if time.time() < self._playout_end - 0.15:
                print("⏭️ [Plivo] early checkpoint ack — still playing, ignoring")
                return
            self.session["bot_speaking"] = False
            self.session["bot_speaking_until"] = 0.0
            self._last_checkpoint_name = None
            self._playout_end = 0.0
            if self._greeting_active:                     # 🔥 NEW
                self._greeting_active = False
                self._greeting_done = True
                print("✅ [GREETING] playback confirmed complete -- barge-in now enabled")
            self._reset_listening()
            self._reset_interrupt_state()
            self._ignore_until = time.time() + ECHO_GRACE_S
            print("👂 [Plivo] playback done (checkpoint ack) — listening")

        elif event == "clearedAudio":
            print("🧹 [Plivo] audio buffer cleared (ack)")

        elif event == "dtmf":
            print(f"🔢 [Plivo] DTMF: {data.get('dtmf', {}).get('digit')}")

        elif event == "media":
            if not self.session.get("stream_sid"):
                self.session["stream_sid"] = data.get("streamId")
            try:
                message = base64.b64decode(data["media"]["payload"])
                if self.recording.get("active"):
                    await self._write_positional(message, source="user")
                await self._process_plivo_audio(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"⚠️ [Plivo] media decode fail: {e}")
                # 🔥 NEW: dialer se aa raha frame hi kharab hai / VAD path
                # crash kar gaya. Ye chupchaap swallow ho raha tha.
                self._log_error("dialer", "media_frame", e, severity="warning")

    # ═══════════════════════════════════════════════════════════
    # VAD HELPERS
    # ═══════════════════════════════════════════════════════════
    def _chunk_rms(self, data: bytes) -> float:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return 0.0
        return np.sqrt(np.mean(samples ** 2))

    def _chunk_ms(self, data: bytes) -> float:
        samples = len(data) // 2
        return (samples / PLIVO_SAMPLE_RATE) * 1000.0

    def _push_pre_roll(self, msg: bytes):
        m = self._chunk_ms(msg)
        self._pre_roll.append((msg, m))
        self._pre_roll_ms += m
        while self._pre_roll_ms > PRE_ROLL_MS and len(self._pre_roll) > 1:
            _, old_m = self._pre_roll.popleft()
            self._pre_roll_ms -= old_m

    def _reset_listening(self):
        self._audio_buffer = bytearray()
        self._speech_started = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._interrupt_ms = 0.0
        self._pre_roll.clear()
        self._pre_roll_ms = 0.0
        self._last_voice_time = None
        # 🔥 warna pichle turn ka signal agle turn ko turant dispatch kar dega
        self._gnani_speech_end = False

    def _reset_interrupt_state(self):
        """Clear any partially-accumulated barge-in progress."""
        self._interrupt_ms = 0.0
        self._interrupt_frame_count = 0
        self._interrupt_voiced_count = 0
        self._interrupt_last_loud_time = None
        self._interrupt_seed = bytearray()

    def _estimate_pitch_hz(self, audio_bytes, sample_rate=PLIVO_SAMPLE_RATE):
        """Rough fundamental-frequency estimate via autocorrelation --
        confirmation signal alongside RMS, not a standalone classifier."""
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < 160:  # ~20ms @8kHz minimum
            return None

        samples = samples - np.mean(samples)
        if np.max(np.abs(samples)) < 1e-6:
            return None

        corr = np.correlate(samples, samples, mode='full')
        corr = corr[len(corr) // 2:]
        if corr[0] <= 0:
            return None

        min_lag = int(sample_rate / INTERRUPT_PITCH_MAX_HZ)
        max_lag = int(sample_rate / INTERRUPT_PITCH_MIN_HZ)
        if max_lag >= len(corr) or min_lag >= max_lag:
            return None

        segment = corr[min_lag:max_lag]
        peak_idx = int(np.argmax(segment))
        peak_val = segment[peak_idx]
        if peak_val < INTERRUPT_PITCH_PERIODICITY_MIN * corr[0]:
            return None

        lag = min_lag + peak_idx
        if lag == 0:
            return None
        return sample_rate / lag

    def _resample_pcm_to_plivo(self, pcm_bytes: bytes, sample_rate: int = MURF_SAMPLE_RATE) -> bytes:
        """One-shot resample. Hot path ab incremental ratecv use karta hai
        (_audio_pump) -- yeh sirf non-streaming callers ke liye bacha hai."""
        try:
            if len(pcm_bytes) % 2 != 0:
                pcm_bytes = pcm_bytes + b'\x00'
            converted, _ = audioop.ratecv(pcm_bytes, 2, 1, sample_rate, PLIVO_SAMPLE_RATE, None)
            return converted
        except Exception as e:
            print(f"⚠️ [Resample] Error: {e}")
            self._log_error("other", "resample_oneshot", e, severity="warning")   # 🔥 NEW
            return b""

    # ═══════════════════════════════════════════════════════════
    # 🔥 RECORDING: positional write
    # ═══════════════════════════════════════════════════════════
    async def _write_positional(self, pcm_bytes: bytes, source: str):
        if not self.recording.get("start_time") or not pcm_bytes:
            return

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

            buf = self.recording[buf_key]
            needed_len = offset + len(pcm_bytes)
            if len(buf) < needed_len:
                buf.extend(b'\x00' * (needed_len - len(buf)))

            buf[offset:offset + len(pcm_bytes)] = pcm_bytes
            self._write_cursor[source] = offset + len(pcm_bytes)

    def _build_recording_basename(self):
        def _clean(value):
            if not value:
                return ""
            value = str(value).strip().upper()
            value = re.sub(r'[^A-Z0-9]+', '_', value)
            return value.strip('_')

        module_code = _clean((self.session.get("cloud_context") or {}).get("module"))
        phone_code = _clean(self.phone_number)
        parts = [p for p in ("PLIVO", module_code, phone_code) if p]
        if not parts:
            parts = ["PLIVO_CALL", self.session_id[:8]]
        return "_".join(parts)

    async def save_conversation_recording(self):
        """Stereo (L=user, R=bot) + safe-averaged mono downmix."""
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

        # --- 1) STEREO WAV: L=user, R=bot ---
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

        # --- 2) MONO downmix — safe averaging, no clipping ---
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

        if _persist_recording_paths_sync is not None:
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

        metadata = {
            "session_id": self.session_id,
            "start_time": self.recording["start_time"],
            "end_time": time.time(),
            "duration_seconds": duration_seconds,
            "transcript": self.recording["transcript"],
            "files": {
                "stereo": stereo_mp3,
                "mixed": mixed_mp3,
            }
        }

        meta_path = os.path.join(temp_dir, f"{filename}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        for p in (stereo_wav, mono_wav):
            if os.path.exists(p):
                os.remove(p)

        return stereo_mp3

    # ═══════════════════════════════════════════════════════════
    # PLIVO AUDIO VAD + PROCESSING
    # ═══════════════════════════════════════════════════════════
    async def _process_plivo_audio(self, message: bytes):
        # Timer fallback for missed checkpoint acks
        if (self.session["bot_speaking"]
                and time.time() > self.session.get("bot_speaking_until", 0) > 0):
            self.session["bot_speaking"] = False
            if self._greeting_active:                     # 🔥 NEW
                self._greeting_active = False
                self._greeting_done = True
                print("✅ [GREETING] fallback timer fired -- barge-in now enabled")
            self._reset_listening()
            self._reset_interrupt_state()
            self._ignore_until = time.time() + ECHO_GRACE_S
            print("👂 [Plivo] playback done (timer) — listening")

        if self._greeting_active and not self._greeting_done:
            return

        energy = self._chunk_rms(message)
        m_ms = self._chunk_ms(message)

        # ═══════ BARGE-IN / INTERRUPT ═══════
        if self.session["bot_speaking"] or self.session["is_processing"]:
            if self._call_ending:
                return
            if not self._greeting_done:        # now effectively unreachable
                return                          # during greeting, but harmless to keep   
            if energy > INTERRUPT_THRESHOLD:
                now = time.time()
                is_voiced = self._estimate_pitch_hz(message) is not None
                self._interrupt_ms += m_ms
                self._interrupt_frame_count += 1
                if is_voiced:
                    self._interrupt_voiced_count += 1
                self._interrupt_last_loud_time = now

                # 🔥 jo awaaz interrupt confirm kar rahi hai, WAHI user ka
                # naya turn hai -- use jama karo, phenko mat. Pehle ye
                # _reset_listening() me udd jaati thi aur STT ko utterance
                # ka onset milta hi nahi tha (kate hue transcripts).
                self._interrupt_seed.extend(message)
                if len(self._interrupt_seed) > BYTES_PER_SEC:   # 1s cap
                    del self._interrupt_seed[:len(self._interrupt_seed) - BYTES_PER_SEC]

                if self._interrupt_ms >= INTERRUPT_MIN_MS:
                    voice_ratio = (
                        self._interrupt_voiced_count / self._interrupt_frame_count
                        if self._interrupt_frame_count else 0.0
                    )
                    if voice_ratio >= INTERRUPT_VOICE_RATIO_THRESHOLD:
                        await self._do_interrupt(voice_ratio)
            else:
                if self._interrupt_last_loud_time is not None:
                    if time.time() - self._interrupt_last_loud_time > INTERRUPT_DIP_GRACE_S:
                        self._reset_interrupt_state()
                else:
                    self._interrupt_ms = 0.0
            return
        if time.time() < self._ignore_until:
            return

        # 🔥 Gnani ko HAR chunk do, VAD gate se PEHLE. Uski server-side VAD
        # tabhi sahi chalti hai jab stream continuous ho -- pehle hum sirf
        # speech-start ke BAAD feed karte the, isliye pacing loop beech ke
        # gaps me silence bhar deta tha aur Gnani ki VAD adhoori stream par
        # chalti thi. feed() turn ke bahar bhi safe hai: PersistentGnaniSTT
        # khud buffer karta hai aur begin_turn() par wahi audio aage jaata
        # hai (Sarvam par bhi wahi pre-roll ring behaviour hai).
        if self._stt_session:
            self._stt_session.feed(message)

        # ═══════ VAD LOGIC ═══════
        if energy > SPEECH_THRESHOLD:
            if not self._speech_started:
                self._speech_ms += m_ms
                if self._speech_ms >= SPEECH_MIN_MS:
                    self._speech_started = True
                    self._gnani_speech_end = False
                    print("🎙️ [VAD] speech start")

                    self._stt_session.begin_turn()
                    print(f"🎙️ [STT] backend={self._stt_session.backend}")

                    asyncio.create_task(
                        database_sync_to_async(set_speech_state)(self.session_id, "speaking")
                    )

                    # NOTE: yahan sirf local _audio_buffer bhara jaata hai --
                    # STT ko ye audio upar wale feed() se PEHLE hi mil chuki
                    # hai. Dobara feed karna double-audio bhej deta.
                    for pr_bytes, _ in self._pre_roll:
                        self._audio_buffer.extend(pr_bytes)
                    self._pre_roll.clear()
                    self._pre_roll_ms = 0.0
                else:
                    self._push_pre_roll(message)
                    return
            self._silence_ms = 0.0
            self._audio_buffer.extend(message)

        elif self._speech_started:
            self._silence_ms += m_ms
            self._audio_buffer.extend(message)

        else:
            self._speech_ms = 0.0
            self._push_pre_roll(message)
            return

        # ═══════ PROCESS CHECK ═══════
        # 🔥 Gnani ka apna speech-end signal sabse pehle aata hai -- uspar
        # turant dispatch karo. _silence_ms wala gate ab sirf safety net hai
        # (Gnani dead ho / Google fallback par ho to bhi turn nikalna chahiye).
        gnani_ended = self._gnani_speech_end
        should_process = (
            self._speech_started
            and (gnani_ended or self._silence_ms >= SILENCE_END_MS)
            and len(self._audio_buffer) >= MIN_SPEECH_BYTES
        ) or len(self._audio_buffer) >= MAX_BUFFER

        if not should_process:
            return

        self.session["is_processing"] = True
        if gnani_ended:
            print(f"⚡ [VAD] Gnani speech-end — early dispatch "
                  f"(local silence tha sirf {self._silence_ms:.0f}ms)")
        else:
            print("🎤 [VAD] utterance done (local VAD fallback)")

        stt_session = self._stt_session
        utterance = bytes(self._audio_buffer)
        self._reset_listening()

        # 🔥 FIX: turn ko BACKGROUND task me chalao. Pehle yahan
        # `await self._process_plivo_utterance(...)` tha -- Channels ek
        # consumer ke messages sequentially handle karta hai, isliye poore
        # STT→LLM→TTS ke dauraan agla media frame receive() me aata hi
        # nahi tha aur barge-in kabhi trigger nahi ho paata tha.
        self._track(self._run_turn(utterance, stt_session))

    async def _run_turn(self, audio_bytes, stt_session):
        try:
            await self._process_plivo_utterance(audio_bytes, stt_session)
        finally:
            self.session["is_processing"] = False

    async def _do_interrupt(self, voice_ratio=0.0):
        print(f"🚨 [INTERRUPT] {self._interrupt_ms:.0f}ms voice_ratio={voice_ratio:.2f}")

        # 🔥 seed ko reset se PEHLE bacha lo -- _reset_interrupt_state()
        # ise clear kar deta hai.
        seeded = bytes(self._interrupt_seed)

        self.session["bot_speaking"] = False
        self.session["bot_speaking_until"] = 0.0
        self.session["interrupt_count"] += 1
        self._reset_interrupt_state()
        self._last_checkpoint_name = None
        self._playout_end = 0.0

        await self._flush_playback_buffer()
        await self._cancel_current_turn()
        await self._flush_playback_buffer()

        self.session["is_processing"] = False
        self._reset_listening()
        self._ignore_until = 0.0

        self._interrupt_pending = {"cut_off_text": self._current_turn_partial_text}
        self._current_turn_partial_text = ""

        # 🔥 interrupt karne wali awaaz hi naya turn hai -- VAD/STT ko
        # seedha usi se seed karo, warna user ko dobara bolna padta hai
        # (ya transcript ka shuruaati hissa gayab ho jaata hai).
        if seeded:
            self._speech_started = True
            self._speech_ms = SPEECH_MIN_MS
            self._silence_ms = 0.0
            self._audio_buffer.extend(seeded)
            try:
                self._stt_session.begin_turn()
                print(f"🎙️ [STT] backend={self._stt_session.backend} (barge-in seed)")
                self._stt_session.feed(seeded)
            except Exception as e:
                print(f"⚠️ [INTERRUPT] STT seed failed: {e}")
                # 🔥 NEW: seed fail hone ka matlab user ka barge-in wala
                # utterance STT tak pahuncha hi nahi -- turn adhoora suna
                # jaayega. Chupchaap nahi jaana chahiye.
                self._log_error("stt", "barge_in_seed", e, severity="warning",
                                seed_bytes=len(seeded))
            asyncio.create_task(
                database_sync_to_async(set_speech_state)(self.session_id, "speaking")
            )
            print(f"🎙️ [VAD] seeded from barge-in ({len(seeded)} bytes)")

    # ═══════════════════════════════════════════════════════════
    # 🔥 STREAMING TTS PIPELINE (schedule + single ordered pump)
    # ═══════════════════════════════════════════════════════════
    async def _tts_producer_queue(self, text, cancel_event, use_cache=False):
        """Synthesis ko background thread me start karta hai aur chunks
        queue me daalta hai. Cache hit par TTS engine call hi nahi hota.

        🔥 CHANGED: ab (queue, was_cached) tuple return karta hai. was_cached
        ka ek hi kaam hai -- TTS ka paisa lagaana hai ya nahi. Cached PCM
        par Murf ko koi request nahi jaati, isliye uska cost 0 hai.
        Caller: _schedule_tts().
        """
        loop = asyncio.get_event_loop()
        q = asyncio.Queue()

        if use_cache and load_cached_pcm is not None:
            try:
                cached = load_cached_pcm(text)
            except Exception:
                cached = None
            if cached:
                print(f"⚡ [FILLER-CACHE] HIT ({len(cached)} bytes) for '{text[:40]}'")

                def producer_cached():
                    CHUNK = 4096
                    for i in range(0, len(cached), CHUNK):
                        if cancel_event.is_set():
                            break
                        loop.call_soon_threadsafe(q.put_nowait, cached[i:i + CHUNK])
                    loop.call_soon_threadsafe(q.put_nowait, None)

                loop.run_in_executor(None, producer_cached)
                return q, True              # 🔥 cached -- koi TTS cost nahi
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
                # tha (sirf finally tha, except nahi) -- turn bina audio ke
                # khatam ho jaata aur caller ko silence sunai deti.
                print(f"❌ [TTS] synthesize_stream failed: {e}")
                logger.error(f"[TTS] synthesize_stream failed: {e}")
                loop.call_soon_threadsafe(
                    self._log_error, "tts", "synthesize_stream", e,
                )
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(None, producer)
        return q, False                     # 🔥 live synthesis -- cost lagega

    async def _schedule_tts(self, text, is_first_sentence=False, use_cache=False,
                            is_filler=False):
        """Synthesis abhi start kar do (parallel), lekin bhejne ka kaam
        _audio_pump strictly isi order me karega jis order me schedule hua."""
        cancel_event = threading.Event()
        # 🔥 CHANGED: _tts_producer_queue ab (queue, was_cached) deta hai.
        queue, was_cached = await self._tts_producer_queue(text, cancel_event, use_cache=use_cache)
        if not was_cached:
            # 🔥 NEW: sirf live-synthesized text ke characters count hote hain.
            # Turn ke aakhir me cost_tts() ke through save_turn() me jaata hai.
            self._turn_tts_chars += len(text or "")
        item = {
            "text": text,
            "queue": queue,
            "cancel_event": cancel_event,
            "is_first_sentence": is_first_sentence,
            "is_filler": is_filler,
            "start_time": time.time(),
        }
        async with self._turn_items_lock:
            self._turn_items.append(item)
        return item

    
    async def _plivo_send_pcm(self, piece: bytes):
        """Ek 8kHz PCM chunk Plivo ko bhejo -- REAL-TIME PACED.

        🔥 Pehle poora reply ek burst me Plivo ke buffer me chala jaata
        tha (5 sec ki audio ~200ms me). Uske baad turn khatam, pump exit,
        is_processing=False -- yaani barge-in ke waqt cancel karne ko kuch
        bacha hi nahi hota tha, sirf clearAudio bharosa tha. Ab hum kabhi
        PLAYOUT_LEAD_S se zyada aage nahi bhejte, isliye barge-in par
        pump beech me hi mar jaata hai aur bot turant chup ho jaati hai.
        """
        now = time.time()
        if self._playout_end < now:
            self._playout_end = now

        # aage bahut bhej diya? ruk jao.
        ahead = self._playout_end - now
        if ahead > PLAYOUT_LEAD_S:
            await asyncio.sleep(ahead - PLAYOUT_LEAD_S)

        if self._closed:
            return

        if self.recording.get("active"):
            await self._write_positional(piece, source="bot")

        await self.safe_send(text_data=json.dumps({
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-l16",
                "sampleRate": PLIVO_SAMPLE_RATE,
                "payload": base64.b64encode(piece).decode(),
            },
        }))

        self._playout_end += len(piece) / BYTES_PER_SEC
        self.session["bot_speaking"] = True
        self.session["bot_speaking_until"] = self._playout_end + 0.5

    async def _audio_pump(self, timing_tracker: dict):
        """🔥 EK HI jagah jahan bot ka audio Plivo ko jaata hai.

        - chunks aate hi incremental ratecv se 24k->8k resample + send
        - real-time paced (dekho _plivo_send_pcm)
        - checkpoint SIRF EK BAAR, poore turn ke aakhir me -- warna har
          item ka ack bot_speaking ko galat waqt par False kar deta tha
        """
        idx = 0
        total_bytes = 0
        sent_anything = False

        try:
            while True:
                while True:
                    async with self._turn_items_lock:
                        if idx < len(self._turn_items):
                            item = self._turn_items[idx]
                            break
                        if self._turn_items_done:
                            raise StopAsyncIteration
                    await asyncio.sleep(0.005)

                rate_state = None
                out = bytearray()
                first = True
                first_piece_sent = False
                tts_start = item["start_time"]
                _leftover = b""
                while True:
                    chunk = await item["queue"].get()
                    if chunk is None:
                        break
                    if self._closed:
                        return
                    if item["cancel_event"].is_set():
                        continue
                    chunk = _leftover + chunk
                    if len(chunk) % 2:
                        chunk += b"\x00"
                    else:
                        _leftover = b""
                    if not chunk:
                        continue
                    try:
                        pcm, rate_state = audioop.ratecv(
                            chunk, 2, 1, MURF_SAMPLE_RATE, PLIVO_SAMPLE_RATE, rate_state
                        )
                    except Exception as e:
                        print(f"⚠️ [Resample] chunk error: {e}")
                        # 🔥 NEW: ek chunk drop hua -- audio me chhota gap
                        # aayega. Baar-baar ho to sample-rate mismatch ka
                        # signal hai (Murf ne rate badal diya, etc).
                        self._log_error("other", "resample_chunk", e, severity="warning",
                                        chunk_bytes=len(chunk))
                        continue
                    out.extend(pcm)

                    while len(out) >= PLIVO_CHUNK_BYTES:
                        if not first_piece_sent and len(out) < TTS_PREBUFFER_BYTES:
                            break
                        piece = bytes(out[:PLIVO_CHUNK_BYTES])
                        del out[:PLIVO_CHUNK_BYTES]
                        await self._plivo_send_pcm(piece)
                        total_bytes += len(piece)
                        sent_anything = True

                        if first:
                            first = False
                            if timing_tracker is not None:
                                if item["is_filler"]:
                                    timing_tracker.setdefault('filler_first_chunk_at', time.time())
                                elif timing_tracker.get('tts_first_audio_ms') is None:
                                    timing_tracker['tts_first_audio_ms'] = (time.time() - tts_start) * 1000
                                if item["is_first_sentence"] and timing_tracker.get('real_user_heard_at') is None:
                                    timing_tracker['real_user_heard_at'] = time.time()
                            if not item["is_filler"]:
                                self._spoken_texts_this_turn.append(item["text"])
                                self._current_turn_partial_text = " ".join(
                                    self._spoken_texts_this_turn
                                ).strip()

                if out and not item["cancel_event"].is_set():
                    rem = len(out) % 320
                    if rem:
                        out.extend(b"\x00" * (320 - rem))
                    await self._plivo_send_pcm(bytes(out))
                    total_bytes += len(out)
                    sent_anything = True
                    if first and not item["is_filler"]:
                        self._spoken_texts_this_turn.append(item["text"])
                        self._current_turn_partial_text = " ".join(
                            self._spoken_texts_this_turn
                        ).strip()

                idx += 1

        except StopAsyncIteration:
            pass
        finally:
            if timing_tracker is not None:
                timing_tracker['tts_total_bytes'] = total_bytes

        # 🔥 checkpoint sirf ek baar, sab kuch bhejne ke BAAD
        if sent_anything and not self._closed:
            name = f"m_{uuid.uuid4().hex[:6]}"
            self._last_checkpoint_name = name
            await self.safe_send(text_data=json.dumps({
                "event": "checkpoint",
                "streamId": self.session["stream_sid"],
                "name": name,
            }))
            
            
    async def _speak_standalone(self, text: str, use_cache=False, tag="misc"):
        """Greeting / reprompt jaise turn-ke-bahar ke lines. Apna chhota
        pump chalata hai taaki wahi streaming path reuse ho.

        🔥 COST: in lines ka apna ConversationTurn row nahi banta (greeting
        to CallSession banne se pehle hi bol di jaati hai). Isliye jo chars
        Murf ko gaye wo _pending_tts_chars me park kar dete hain --
        _consume_tts_chars() unhe AGLE bot turn ke saath DB me daal dega.
        Warna har call ka greeting ~100 char chupchaap unbilled reh jaata.
        """
        if not text or self._closed:
            return
        self._turn_items = []
        self._turn_items_done = False
        self._turn_tts_chars = 0        # 🔥 fresh count -- neeche park kar denge
        timing = {}
        pump = self._track(self._audio_pump(timing))
        await self._schedule_tts(text, is_first_sentence=True, use_cache=use_cache)
        self._turn_items_done = True
        try:
            await pump
        except asyncio.CancelledError:
            raise
        finally:
            # 🔥 chahe pump cancel ho gaya ho, Murf ko request ja chuki hai
            # aur uska paisa lag chuka hai -- isliye finally me park karo.
            self._pending_tts_chars += self._turn_tts_chars
            self._turn_tts_chars = 0
        print(f"🔊 [{tag.upper()}] spoken ({timing.get('tts_total_bytes', 0)} bytes)")

    # ═══════════════════════════════════════════════════════════
    # STT → LLM → TTS (Full Turn Processing)
    # ═══════════════════════════════════════════════════════════
    async def _process_plivo_utterance(self, audio_bytes: bytes, stt_session=None):
        """Streaming-STT → Streaming LLM → Streaming TTS with timing tracking"""

        timing = {
            'audio_received_at': time.time(),
            'stt_done_at': None,
            'llm_first_token_at': None,
            'llm_complete_at': None,
            'tts_first_audio_ms': None,
            'tts_total_ms': None,
            'tts_total_bytes': 0,
            'user_heard_at': None,
            'real_user_heard_at': None,
        }
        pump_task = None
        self._turn_tts_chars = 0        # 🔥 NEW: har turn fresh TTS char count
        
        

        try:
            print(f"\n{'='*60}")
            print(f"🎤 [TURN START] New user turn")
            print(f"{'='*60}")

            # ═══════════════════════════════════════════════════
            # STEP 1: STT
            # ═══════════════════════════════════════════════════
            if stt_session is not None:
                speech_duration_estimate = len(audio_bytes) / BYTES_PER_SEC
                gnani_timeout = min(1.5, max(0.5, speech_duration_estimate + 0.4))
                try:
                    transcript = await stt_session.end_turn(rescue_audio=audio_bytes, timeout=gnani_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 🔥 NEW: STT provider down / socket dead / rescue bhi
                    # fail. Turn khaali transcript ke saath aage badhega
                    # (neeche _handle_empty_transcript reprompt kar dega),
                    # par DB me pata chalega ki kaun gira.
                    print(f"❌ [STT] end_turn failed: {e}")
                    logger.error(f"[STT] end_turn failed (session={self.session_id}): {e}")
                    self._log_error("stt", "end_turn", e, severity="error",
                                    backend=getattr(stt_session, "backend", None),
                                    audio_bytes=len(audio_bytes))
                    transcript = ""
            else:
                print("⚠️ [STT] no streaming session attached, empty transcript")
                transcript = ""

            timing['stt_done_at'] = time.time()
            stt_latency = (timing['stt_done_at'] - timing['audio_received_at']) * 1000

            if stt_session is not None and stt_session.first_result_time and stt_session.start_time:
                timing['stt_first_token_ms'] = round(
                    (stt_session.first_result_time - stt_session.start_time) * 1000, 1
                )
            else:
                timing['stt_first_token_ms'] = None

            if not transcript:
                print(f"❌ [STT] No transcript found")
                await self._handle_empty_transcript()
                return

            self._customer_text_history.append(transcript)

            print(f"📝 [STT] '{transcript}'")
            print(f"⏱️ [STT] Audio → Text: {stt_latency:.0f}ms")

            self.session["empty_count"] = 0
            self.session["last_transcript"] = transcript
            self.session["has_conversation"] = True

            self._current_turn_record = {
                "timestamp": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
                "user": transcript,
                "bot": "",
            }

            asyncio.create_task(
                database_sync_to_async(set_final_transcript)(self.session_id, transcript)
            )

            history = await database_sync_to_async(get_history_for_llm)(self.session_id)
            await database_sync_to_async(save_conversation)(self.session_id, "Customer", transcript)

            # 🔥 NEW: STT cost isi customer row par. audio_bytes PCM16 mono
            # @8kHz hai (Plivo) -- sample_rate DENA zaroori hai, warna
            # default 16000 se cost aadha count hoga.
            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "customer", transcript,
                    stt_pricing=cost_stt_from_bytes(len(audio_bytes),
                                                    sample_rate=PLIVO_SAMPLE_RATE),
                )
            )

            interrupted_context = self._interrupt_pending
            self._interrupt_pending = None
            self._current_turn_partial_text = ""
            self._spoken_texts_this_turn = []

            is_cut_call = any(k in transcript.lower() for k in CUT_CALL_KEYWORDS)

            # ═══════════════════════════════════════════════════
            # STEP 2: pump start + FILLER (ordered, cache-first)
            # ═══════════════════════════════════════════════════
            self._turn_items = []
            self._turn_items_done = False
            pump_task = self._track(self._audio_pump(timing))
            if not is_cut_call:
                filler_text, filler_cacheable = pick_filler_detailed(
                    transcript,
                    customer_name=self.session["cloud_context"].get("customer_name", "").split()[0],
                )
                timing['filler_requested_at'] = time.time()
                await self._schedule_tts(
                    filler_text, is_first_sentence=True,
                    use_cache=filler_cacheable, is_filler=True,
                )
                print(f"🗣️ [FILLER] '{filler_text}' scheduled (cached={filler_cacheable})")
            # ═══════════════════════════════════════════════════
            # STEP 2.5: SERVICE BOOKING (plain Python, not the LLM)
            # ═══════════════════════════════════════════════════
            reference_context = None
            pending = self.session.get("pending_slot")

            if pending and mentions_confirmation(transcript):
                booking_result = await database_sync_to_async(book_slot_for_session)(
                    self.session_id, pending["date"], pending["time"],
                )
                self.session["pending_slot"] = None
                if booking_result.get("success"):
                    reference_context = (
                        f"BOOKING CONFIRMED for {pending['date']} at {pending['time']}. "
                        f"Tell the customer their service appointment is booked."
                    )
                elif booking_result.get("error") == "slot_taken":
                    alt = booking_result.get("next_available")
                    reference_context = (
                        f"That slot ({pending['time']} on {pending['date']}) was just taken by "
                        f"another booking. Next open slot that day is {alt or 'none left that day'} "
                        f"-- offer it to the customer."
                    )
                else:
                    # 🔥 NEW: booking_failed / invalid date / no_branch --
                    # customer ko maafi wali line jaayegi, aur error DB me.
                    self._log_error(
                        "booking", "book_slot_for_session",
                        booking_result.get("error") or "unknown booking failure",
                        severity="error",
                        slot_date=pending.get("date"), slot_time=pending.get("time"),
                    )
                    reference_context = (
                        "The booking attempt failed due to a system error -- apologize and "
                        "offer to try again."
                    )
            else:
                slot_date, slot_time, slot_time_was_rounded = await database_sync_to_async(extract_slot_request)(transcript)
                if slot_date:
                    slots = await database_sync_to_async(get_available_slots)(slot_date)
                    reference_context = await database_sync_to_async(format_slots_for_reference)(
                        slot_date, slots
                    )
                    if slot_time:
                        match = next((s for s in slots if s["time"] == slot_time), None)
                        if match and match["status"] == "open":
                            self.session["pending_slot"] = {
                                "date": slot_date.isoformat(), "time": slot_time,
                            }
                            reference_context += (
                                f". {slot_time} is open -- confirm this slot with the "
                                f"customer before booking it."
                            )
                        else:
                            reference_context += f". {slot_time} is already booked -- suggest an open slot."

            # ═══════════════════════════════════════════════════
            # STEP 3: LLM STREAMING + TTS STREAMING
            # ═══════════════════════════════════════════════════
            cloud_context = {
                **self.session["cloud_context"],
                "today": timezone.now().date().strftime("%Y-%m-%d (%A)"),
                "current_datetime_ist": _now_ist().strftime("%Y-%m-%d %H:%M"),
                "customer_history_summary": self._customer_text_history,
            }

            full_response = ""
            first_token_time = None
            llm_start_time = time.time()

            text_buffer = ""
            first_sentence_sent = False

            print(f"\n🤖 [LLM] Starting stream...")

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, True))

            async for chunk in self._llm_stream_async(
                self.session_id, transcript, cloud_context,
                reference_context=reference_context, history=history,
                interrupted_context=interrupted_context,
            ):
                if first_token_time is None:
                    first_token_time = time.time()
                    timing['llm_first_token_at'] = first_token_time
                    ttft = (first_token_time - llm_start_time) * 1000
                    print(f"⚡ [LLM] First token: {ttft:.0f}ms")

                full_response += chunk
                text_buffer += chunk
                print(f"📝 [CHUNK] {chunk}", end="", flush=True)

                candidate = text_buffer.strip()
                if not candidate:
                    continue

                ends_sentence = chunk.endswith(('.', '?', '!', '।', '\n'))
                ends_clause = chunk.endswith((',', '—', ';')) or (
                    chunk.endswith(':') and not _is_mid_number_colon(candidate)
                )

                if not first_sentence_sent:
                    # 🔥 LATENCY: pehle sentence ke liye poore '।' ka wait
                    # mat karo -- itna text kaafi hai to turant TTS bhejo.
                    should_flush = (
                        (ends_sentence or ends_clause) and len(candidate) >= MIN_TTS_CHARS_FIRST
                    ) or len(candidate) >= MIN_TTS_CHARS_FIRST * 3
                else:
                    # Baad ke sentences batch karo -- har chhote sentence par
                    # naya Murf stream kholna = prosody ka cold restart + delay.
                    should_flush = ends_sentence and len(candidate) >= MIN_TTS_CHARS_AFTER_FIRST

                if should_flush:
                    text_buffer = ""
                    print(f"\n🔥 [TTS] Chunk ready: {candidate[:50]}...")
                    await self._schedule_tts(
                        candidate, is_first_sentence=not first_sentence_sent
                    )
                    first_sentence_sent = True

            if text_buffer.strip():
                print(f"\n🔥 [TTS] Final buffer: {text_buffer[:50]}...")
                await self._schedule_tts(
                    text_buffer.strip(), is_first_sentence=not first_sentence_sent
                )
                first_sentence_sent = True

            # 🔥 NEW: LLM ne kuch bola hi nahi -- provider down tha ya stream
            # khaali aayi. _llm_stream_async() ne exception already log kar
            # di hogi (agar exception tha); ye us case ke liye hai jahan
            # stream chup-chaap khaali aayi.
            if not full_response.strip():
                self._log_error("llm", "empty_response",
                                "LLM stream produced no text this turn",
                                severity="error", transcript=transcript[:200])

            # 🔥 TIMING: llm_complete_at ko pump ke AWAIT se PEHLE set karo.
            # pump ab real-time paced hai (PLAYOUT_LEAD_S) -- wo poore
            # playback ka wait karta hai. Baad me set karne se report ke
            # "LLM First Token → LLM Complete" me poora audio duration jud
            # jaata tha aur TOTAL 5000ms+ dikhne lagta tha.
            timing['llm_complete_at'] = time.time()
            llm_total = (timing['llm_complete_at'] - llm_start_time) * 1000
            print(f"\n⏱️ [LLM] Total LLM time: {llm_total:.0f}ms")

            self._turn_items_done = True
            await pump_task
            pump_task = None

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, False))

            await database_sync_to_async(save_conversation)(self.session_id, "Aarohi", full_response)
            self._current_turn_partial_text = ""

            if self._current_turn_record is not None:
                self._current_turn_record["bot"] = full_response
                self.recording["transcript"].append(self._current_turn_record)
                self._current_turn_record = None

            timing['user_heard_at'] = time.time()

            # 🔥 NEW: TTS cost -- `await pump_task` ke BAAD, isliye is turn
            # ke saare _schedule_tts() calls ho chuke hain aur count final
            # hai. _consume_tts_chars() greeting/reprompt ke park kiye hue
            # chars bhi jod deta hai aur dono counters reset kar deta hai.
            turn_tts_cost = cost_tts(self._consume_tts_chars())

            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "bot", full_response,
                    timing=build_timing_record(timing),
                    tts_pricing=turn_tts_cost,          # 🔥 NEW
                )
            )

            self._print_timing_report(timing)

            await self.safe_send(text_data=json.dumps({
                "event": "ai_response",
                "text": full_response,
                "back_flag": 150 if is_cut_call else 2,
            }))

            if is_cut_call:
                print("📞 [Plivo] Call end requested")
                wait_s = max(self.session.get("bot_speaking_until", 0) - time.time(), 0)
                await asyncio.sleep(min(wait_s, 10))
                await self.close()

        except asyncio.CancelledError:
            async with self._turn_items_lock:
                for item in self._turn_items:
                    item["cancel_event"].set()
            self._turn_items_done = True

            spoken = (self._current_turn_partial_text or "").strip()
            if spoken:
                print(f"🚫 [Plivo] turn cancelled -- spoken so far: '{spoken[:60]}...'")
                try:
                    await database_sync_to_async(save_conversation)(
                        self.session_id, "Aarohi", spoken
                    )
                    # 🔥 NEW: barge-in par bhi TTS ka paisa lag chuka hai --
                    # jo text Murf ko ja chuka wo bill ho gaya, chahe caller
                    # ne poora suna ho ya nahi. Isliye yahan bhi save karo.
                    asyncio.create_task(
                        database_sync_to_async(save_turn)(
                            self.session_id, "bot", spoken,
                            tts_pricing=cost_tts(self._consume_tts_chars()),
                        )
                    )
                    if self._current_turn_record is not None:
                        self._current_turn_record["bot"] = spoken
                        self.recording["transcript"].append(self._current_turn_record)
                        self._current_turn_record = None
                except Exception as save_err:
                    print(f"⚠️ [Plivo] failed to persist interrupted spoken text: {save_err}")
                    # 🔥 NEW
                    self._log_error("db", "save_turn(interrupted)", save_err, severity="warning")
            else:
                print("🚫 [Plivo] turn cancelled (caller disconnected) -- nothing spoken yet")
                self._current_turn_record = None
            raise
        except asyncio.TimeoutError as e:
            print("❌ [STT] timeout")
            self._log_error("stt", "turn_timeout", e, severity="error")     # 🔥 NEW
        except Exception as e:
            print(f"❌ [Plivo] Error: {e}")
            import traceback
            traceback.print_exc()
            # 🔥 NEW: turn ka koi bhi unhandled crash -- ye "bot beech me
            # band ho gaya" wala case hai, isliye critical.
            logger.exception(f"[Plivo] turn crashed (session={self.session_id}): {e}")
            self._log_error("other", "_process_plivo_utterance", e, severity="critical")
        finally:
            self._turn_items_done = True
            if pump_task is not None and not pump_task.done():
                try:
                    await pump_task
                except Exception:
                    pass

            end_call_signal = should_end_call(self.session_id)
            if end_call_signal and not self._closed:
                print(f"👋 [END-CALL] LLM called end_call "
                    f"(reason={end_call_signal.get('reason')}) -- closing socket")
                wait_s = max(self.session.get("bot_speaking_until", 0) - time.time(), 0)
                await asyncio.sleep(min(wait_s, 10))
                try:
                    await self.close()
                except Exception as e:
                    print(f"⚠️ [END-CALL] close() raised (socket likely already gone): {e}")

    # ═══════════════════════════════════════════════════════════
    # HANDLE EMPTY TRANSCRIPT
    # ═══════════════════════════════════════════════════════════
    async def _handle_empty_transcript(self):
        self.session["empty_count"] += 1
        print(f"⚠️ [STT] empty ({self.session['empty_count']})")

        if self.session["empty_count"] >= 2:
            self.session["empty_count"] = 0
            r_text = REPROMPT_TEXTS[self.session["reprompt_idx"] % len(REPROMPT_TEXTS)]
            self.session["reprompt_idx"] += 1
            await self._speak_standalone(r_text, use_cache=True, tag="reprompt")

    # ═══════════════════════════════════════════════════════════
    # 🔥 NEW: SILENCE WATCHDOG — customer + LLM dono chup, bot ko
    # proactively bolna chahiye. Do escalating steps:
    #   1) SILENCE_CHECKIN_S ki chup ke baad "kya hua sir, hold par hain
    #      kya" jaisi line
    #   2) uske BAAD bhi SILENCE_DISCONNECT_S chup rahe to polite goodbye
    #      bolke call kaat do
    # ═══════════════════════════════════════════════════════════
    async def _silence_watchdog(self):
        try:
            while not self._closed:
                await asyncio.sleep(SILENCE_WATCHDOG_TICK_S)
                if self._closed:
                    return

                # bot bol raha hai ya turn process ho raha hai -- idle nahi hai
                if self.session.get("bot_speaking") or self.session.get("is_processing"):
                    self._listening_since = None
                    continue

                # greeting abhi khatam nahi hui -- count mat karo
                if not self._greeting_done:
                    continue

                # user bol raha hai (VAD ne speech pakda hai) -- idle nahi
                if self._speech_started:
                    self._listening_since = None
                    self._silence_stage = 0
                    continue

                if self._listening_since is None:
                    self._listening_since = time.time()
                    continue

                idle_s = time.time() - self._listening_since

                if self._silence_stage == 0 and idle_s >= SILENCE_CHECKIN_S:
                    self._silence_stage = 1
                    self._listening_since = None   # check-in bolne ke baad se dobara ginenge
                    self._track(self._handle_silence_checkin())

                elif self._silence_stage == 1 and idle_s >= SILENCE_DISCONNECT_S:
                    self._silence_stage = 2
                    self._track(self._handle_silence_disconnect())
                    return   # kaam ho gaya, watchdog ki zaroorat nahi ab
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ [SILENCE] watchdog crashed: {e}")
            self._log_error("other", "silence_watchdog", e, severity="warning")

    async def _handle_silence_checkin(self):
        text = random.choice(SILENCE_CHECKIN_TEXTS)
        print(f"🤫 [SILENCE] {SILENCE_CHECKIN_S:.0f}s se chup -- check-in: '{text}'")
        try:
            await self._speak_standalone(text, use_cache=True, tag="silence_checkin")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ [SILENCE] checkin failed: {e}")
            self._log_error("tts", "silence_checkin", e, severity="warning")

    async def _handle_silence_disconnect(self):
        print(f"🤫 [SILENCE] checkin ke baad bhi {SILENCE_DISCONNECT_S:.0f}s chup -- "
              f"politely disconnecting")
        try:
            await self._speak_standalone(SILENCE_GOODBYE_TEXT, use_cache=True, tag="silence_goodbye")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ [SILENCE] goodbye line failed: {e}")
            self._log_error("tts", "silence_goodbye", e, severity="warning")

        if self._closed:
            return
        wait_s = max(self.session.get("bot_speaking_until", 0) - time.time(), 0)
        await asyncio.sleep(min(wait_s, 6))
        if not self._closed:
            try:
                await self.close()
            except Exception as e:
                print(f"⚠️ [SILENCE] close() raised: {e}")

    # ═══════════════════════════════════════════════════════════
    # STREAMING LLM (Async wrapper)
    # ═══════════════════════════════════════════════════════════
    async def _llm_stream_async(self, session_id, customer_text, context, reference_context=None,
                                 history=None, interrupted_context=None):
        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def producer():
            try:
                for chunk in chat_turn_stream(
                    session_id=session_id,
                    customer_text=customer_text,
                    context=context,
                    use_rag=True,
                    reference_context=reference_context,
                    history=history,
                    interrupted_context=interrupted_context,
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
            chunk = await queue.get()
            if chunk is None:
                break
            if self._closed:
                break
            yield chunk

    # ═══════════════════════════════════════════════════════════
    # GREETING
    # ═══════════════════════════════════════════════════════════
    async def _send_greeting(self):
        ctx = self.session.get("cloud_context", {})
        customer_name = ctx.get("customer_name", "Customer")
        vehicle_model = ctx.get("vehicle_model", "Unknown")
        branch_name = ctx.get("branch")
        if not branch_name or branch_name == "Unknown":
            branch_name = None

        dealer_intro = f"{branch_name}" if branch_name else "ओम होंडा"

        if customer_name and customer_name != "Customer":
            greeting = f"नमस्ते {customer_name} जी! मैं {dealer_intro} से आरोही बोल रही हूँ।"
            if vehicle_model and vehicle_model != "Unknown":
                greeting += f" आपकी {vehicle_model} की सर्विस के बारे में बात करनी थी।"
        else:
            greeting = f"नमस्ते जी! मैं {dealer_intro} से आरोही बोल रही हूँ।"
        print(f"🗣️ [GREETING] {greeting}")
        self._greeting_active = True
        await database_sync_to_async(save_conversation)(self.session_id, "Aarohi", greeting)
        try:
            await self._speak_standalone(greeting, use_cache=True, tag="greeting")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ [GREETING] failed: {e}")
            logger.error(f"[GREETING] failed (session={self.session_id}): {e}")
            self._log_error("tts", "greeting", e, severity="critical",
                            greeting_chars=len(greeting))

    # ═══════════════════════════════════════════════════════════
    # mp3 fallback (stale/unused for the raw-PCM path)
    # ═══════════════════════════════════════════════════════════
    def _mp3_to_pcm(self, mp3_bytes: bytes) -> bytes:
        try:
            from pydub import AudioSegment as _AS
            audio = _AS.from_mp3(io.BytesIO(mp3_bytes))
            audio = audio.set_frame_rate(PLIVO_SAMPLE_RATE).set_channels(1).set_sample_width(2)
            return audio.raw_data
        except Exception as e:
            print(f"❌ [MP3→PCM] Error: {e}")
            import subprocess
            try:
                result = subprocess.run(
                    ["ffmpeg", "-i", "pipe:0", "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1"],
                    input=mp3_bytes,
                    capture_output=True,
                )
                return result.stdout
            except Exception as e2:
                print(f"❌ [FFmpeg fallback] Error: {e2}")
                self._log_error("other", "mp3_to_pcm", e2, severity="warning")   # 🔥 NEW
                return b""

    # ═══════════════════════════════════════════════════════════
    # TIMING REPORT
    # ═══════════════════════════════════════════════════════════
    def _print_timing_report(self, timing: dict):
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

        if timing.get('real_user_heard_at'):
            real_latency = (timing['real_user_heard_at'] - timing['audio_received_at']) * 1000
            print(f"🎯 REAL latency (audio → pehli awaaz):  {real_latency:>8.0f}ms")