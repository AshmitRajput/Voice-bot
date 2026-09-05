import json
import base64
import asyncio
import time
import uuid
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
from .services.cloud_llm_service import chat_turn_stream, build_timing_record, get_last_turn_usage
from .services.tts_service import get_tts_service
from .services.stt_service import STTSession
from .services.conversation_history import (
    init_state, set_speech_state, set_final_transcript, set_generating, clear_state,
    save_conversation,
)
from .views import (
    get_or_create_call_session, save_turn, end_call_session,
    set_dialer_call_id, get_customer_context, finalize_call_summary,
    get_history_for_llm, log_service_error,
    get_active_persona_config, get_active_llm_setting_raw,
)
from decimal import Decimal, ROUND_HALF_UP
from .views_admin import _persist_recording_paths_sync, _resolve_customer_sync
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


logger = logging.getLogger('recovery_agent')

PLIVO_CHUNK_BYTES = 1600
ECHO_GRACE_S = 0.15
INTERRUPT_THRESHOLD = 600
INTERRUPT_MIN_MS = 250
PRE_ROLL_MS = 200
MIN_SPEECH_BYTES = 8000
MAX_BUFFER_SECONDS = 10
BYTES_PER_SEC = 16000
PLIVO_SAMPLE_RATE = 8000
SPEECH_THRESHOLD = 400
SPEECH_MIN_MS = 300
MAX_BUFFER = BYTES_PER_SEC * MAX_BUFFER_SECONDS
SILENCE_END_MS = 700
PLAYOUT_LEAD_S = 1.0
MURF_SAMPLE_RATE = 24000
TTS_PREBUFFER_MS = 300
TTS_PREBUFFER_BYTES = int(BYTES_PER_SEC * TTS_PREBUFFER_MS / 1000)

STT_PER_HOUR = Decimal('27')
USD_TO_INR = Decimal('88')
TTS_USD_PER_1K = Decimal('0.01')
TTS_PER_1K_CHARS = TTS_USD_PER_1K * USD_TO_INR
_COST_Q = Decimal('0.000001')


def _quantize_cost(value):
    return Decimal(value).quantize(_COST_Q, rounding=ROUND_HALF_UP)


def cost_stt_from_bytes(audio_bytes_len, sample_rate=PLIVO_SAMPLE_RATE):
    if not audio_bytes_len:
        return Decimal('0')
    seconds = Decimal(int(audio_bytes_len)) / (sample_rate * 2)
    return _quantize_cost(seconds / 3600 * STT_PER_HOUR)


def cost_tts(chars):
    if not chars:
        return Decimal('0')
    return _quantize_cost(Decimal(int(chars)) / 1000 * TTS_PER_1K_CHARS)


INTERRUPT_PITCH_MIN_HZ = 100
INTERRUPT_PITCH_MAX_HZ = 300
INTERRUPT_PITCH_PERIODICITY_MIN = 0.30
INTERRUPT_VOICE_RATIO_THRESHOLD = 0.40
INTERRUPT_DIP_GRACE_S = 0.25

MIN_TTS_CHARS_FIRST = 40
MIN_TTS_CHARS_AFTER_FIRST = 80
SILENCE_CHECKIN_S = 8.0
SILENCE_DISCONNECT_S = 12.0
SILENCE_WATCHDOG_TICK_S = 1.0

SILENCE_CHECKIN_TEXTS = [
    "क्या हुआ सर, आप हैं कि नहीं? आवाज़ नहीं आ रही।",
    "हेलो सर, क्या आप होल्ड पर हैं? मुझे कुछ सुनाई नहीं दे रहा।",
]
SILENCE_GOODBYE_TEXT = (
    "ठीक है सर, लगता है अभी बात करना मुश्किल है। मैं थोड़ी देर बाद "
    "फिर से कॉल करती हूँ। धन्यवाद, नमस्ते।"
)

STT_PHRASES = ["EMI", "पेमेंट", "यूपीआई", "due date", "callback"]

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
    if not text.endswith(':'):
        return False
    stripped = text[:-1].rstrip()
    return bool(stripped) and stripped[-1].isdigit()


class PlivoDialerConsumer(AsyncWebsocketConsumer):
    """
    WebSocket: Plivo -> STT -> Cloud LLM (recovery agent, tool-calling) ->
    Murf TTS Streaming. Real outbound calls are triggered separately via
    plivo_service.initiate_outbound_call() -- this consumer only handles
    the audio for a call once Plivo has connected it here (see
    views.plivo_answer for the XML that points Plivo at this URL).

    No fast-path intent shortcuts, no keyword-based hangup, no filler
    service, no booking logic -- every turn goes through the LLM +
    tool-calling loop, same as consumers.py. end_call/schedule_callback
    (both terminal tools) are the only way a call ends from the AI side.
    """

    def _on_end_call_signal(self, payload):
        self._call_ending = True
        print(f"🔒 [END-CALL] signalled early (reason={payload.get('reason')})")

    def _log_error(self, provider, stage, exc, severity='error', **context):
        try:
            _fire_and_forget(
                database_sync_to_async(log_service_error)(
                    session_id=getattr(self, 'session_id', None),
                    provider=provider, stage=stage, severity=severity,
                    error_type=type(exc).__name__ if isinstance(exc, BaseException) else 'Error',
                    error_message=str(exc),
                    context={**(context or {}), 'transport': 'plivo'},
                ),
                label=f"errlog:{provider}/{stage}",
            )
        except Exception as e:
            logger.warning(f"[ERRLOG] could not schedule error log ({provider}/{stage}): {e}")

    def _consume_tts_chars(self):
        total = self._turn_tts_chars + self._pending_tts_chars
        self._turn_tts_chars = 0
        self._pending_tts_chars = 0
        return total

    async def connect(self):
        await self.accept()

        url_kwargs = self.scope.get("url_route", {}).get("kwargs", {}) or {}
        self.session_id = url_kwargs.get("session_id") or str(uuid.uuid4())
        self.phone_number = url_kwargs.get("phone") or None
        self.client_type = "plivo"
        self._customer_text_history = []

        self.session = {
            "history": [], "is_processing": False, "bot_speaking": False,
            "bot_speaking_until": 0.0, "stream_sid": None,
            "empty_count": 0, "reprompt_idx": 0, "last_transcript": "",
            "interrupt_count": 0, "has_conversation": False,
            "cloud_context": {},
        }

        llm_setting = await database_sync_to_async(get_active_llm_setting_raw)()
        self.persona_config = await database_sync_to_async(get_active_persona_config)()
        self.persona_name = (self.persona_config or {}).get("name") or "Riya"
        self._tts_voice_name = (
            getattr(llm_setting.voice, "provider_voice_id", None)
            if llm_setting and llm_setting.voice else None
        ) or "hi-IN-sunaina"
        self._tts_provider = "murf"

        self.customer = await database_sync_to_async(_resolve_customer_sync)(
            phone_number=self.phone_number,
        )
        self.customer_id = self.customer.id if self.customer else None

        call_attempt_number, promise_broken = await database_sync_to_async(
            self._resolve_escalation_state
        )(self.customer_id)

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

        self._listening_since = None
        self._silence_stage = 0
        self._silence_watchdog_task = None

        self._interrupt_frame_count = 0
        self._interrupt_voiced_count = 0
        self._interrupt_last_loud_time = None
        self._interrupt_seed = bytearray()

        self._gnani_speech_end = False
        self._last_checkpoint_name = None
        self._playout_end = 0.0

        self._closed = False
        self._active_tasks = set()
        self._stt_connect_task = None

        self._current_turn_partial_text = ""
        self._interrupt_pending = None
        self._spoken_texts_this_turn = []

        self._turn_items = []
        self._turn_items_lock = asyncio.Lock()
        self._turn_items_done = False

        self._turn_tts_chars = 0
        self._pending_tts_chars = 0

        self.RECORD_SAMPLE_RATE = PLIVO_SAMPLE_RATE
        self._write_cursor = {"user": 0, "bot": 0}
        self.recording = {
            "active": False, "user_audio": bytearray(), "bot_audio": bytearray(),
            "start_time": None, "transcript": [],
        }
        self._audio_lock = asyncio.Lock()
        self._current_turn_record = None
        self._call_ending = False
        self._greeting_active = False
        self._greeting_done = False
        register_end_call_handler(self.session_id, asyncio.get_event_loop(), self._on_end_call_signal)

        asyncio.create_task(database_sync_to_async(init_state)(self.session_id))
        asyncio.create_task(
            database_sync_to_async(get_or_create_call_session)(
                self.session_id, phone_number=self.phone_number, customer_id=self.customer_id,
            )
        )

        print(f"🔌 [Plivo] Client connected, session_id={self.session_id}")

        self._stt_session = STTSession(sample_rate=PLIVO_SAMPLE_RATE, phrases=STT_PHRASES)
        self._stt_session.on_speech_end = self._on_gnani_speech_end
        self._stt_connect_task = asyncio.create_task(self._stt_session.connect())

        await self._refresh_customer_context(call_attempt_number, promise_broken)
        self._silence_watchdog_task = asyncio.create_task(self._silence_watchdog())

    @staticmethod
    def _resolve_escalation_state(customer_id):
        if not customer_id:
            return 1, False
        try:
            from .models import CallSession, RecoveryCase
            prior_calls = CallSession.objects.filter(
                customer_id=customer_id, flag='c', status='completed',
            ).count()
            case = RecoveryCase.objects.filter(
                customer_id=customer_id, flag='c',
                status__in=['open', 'in_progress', 'reopened'],
            ).order_by('-created_at').first()
            promise_broken = bool(
                case and case.promise_date and case.promise_date < timezone.localdate()
            )
            return prior_calls + 1, promise_broken
        except Exception as e:
            logger.warning(f"[ESCALATION] resolve failed: {e}")
            return 1, False

    def _on_gnani_speech_end(self):
        if self._speech_started:
            self._gnani_speech_end = True

    async def _refresh_customer_context(self, call_attempt_number, promise_broken):
        try:
            ctx = await database_sync_to_async(get_customer_context)(self.phone_number)
        except Exception as e:
            print(f"❌ [DB] get_customer_context failed: {e}")
            logger.error(f"[DB] get_customer_context failed (session={self.session_id}): {e}")
            self._log_error("db", "get_customer_context", e, severity="error", phone=self.phone_number)
            ctx = {"customer_name": "Customer", "amount_due": "0", "due_date": None, "recovery_status": "no_open_case"}

        self.session["cloud_context"] = {
            "customer_id": self.customer_id,
            "customer_name": ctx.get("customer_name", "Customer"),
            "amount_due": ctx.get("amount_due", "0"),
            "outstanding_amount": ctx.get("amount_due", "0"),
            "due_date": ctx.get("due_date"),
            "recovery_status": ctx.get("recovery_status", "no_open_case"),
            "call_attempt_number": call_attempt_number,
            "promise_broken": promise_broken,
            "workflow": "revenue_recovery",
            "persona_config": self.persona_config,
        }
        set_call_context(self.session_id, phone_number=self.phone_number,
                          customer_name=self.session["cloud_context"]["customer_name"])

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
                self._log_error("recording", "save_conversation_recording", e,
                                severity="error", close_code=close_code)

        async with self._turn_items_lock:
            for item in self._turn_items:
                item["cancel_event"].set()

        pending = [t for t in self._active_tasks if not t.done()]
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
                self._log_error("stt", "session_close", e, severity="warning")

        _fire_and_forget(database_sync_to_async(clear_state)(self.session_id), label="clear_state")
        status = "completed" if self.session.get("has_conversation") else "dropped"
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
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _cancel_current_turn(self):
        async with self._turn_items_lock:
            for item in self._turn_items:
                item["cancel_event"].set()
        pending = [t for t in self._active_tasks if not t.done()]
        if not pending:
            return
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _flush_playback_buffer(self):
        if self.session.get("stream_sid"):
            await self.safe_send(text_data=json.dumps({
                "event": "clearAudio", "streamId": self.session["stream_sid"],
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
            print(f"⚠️ [Plivo] send failed: {e}")

    async def receive(self, text_data=None, bytes_data=None):
        if text_data:
            data = json.loads(text_data)
            if "event" in data:
                await self._handle_plivo_event(data)
        elif bytes_data:
            pass

    async def _handle_plivo_event(self, data):
        event = data.get("event")

        if event == "start":
            self.session["stream_sid"] = data.get("start", {}).get("streamId") or data.get("streamId")
            print(f"📞 [Plivo] Call started: {self.session['stream_sid']}")
            asyncio.create_task(
                database_sync_to_async(set_dialer_call_id)(self.session_id, self.session["stream_sid"])
            )
            self.recording["active"] = True
            self.recording["start_time"] = time.time()
            self._track(self._send_greeting())

        elif event == "stop":
            print("🔌 [Plivo] Call ended")
            asyncio.create_task(database_sync_to_async(end_call_session)(self.session_id, "completed"))
            asyncio.create_task(
                database_sync_to_async(finalize_call_summary)(self.session_id, self.session.get("cloud_context"))
            )
            await self.close()

        elif event == "playedStream":
            name = data.get("name") or data.get("playedStream", {}).get("name")
            if self._last_checkpoint_name and name and name != self._last_checkpoint_name:
                return
            if time.time() < self._playout_end - 0.15:
                return
            self.session["bot_speaking"] = False
            self.session["bot_speaking_until"] = 0.0
            self._last_checkpoint_name = None
            self._playout_end = 0.0
            if self._greeting_active:
                self._greeting_active = False
                self._greeting_done = True
                print("✅ [GREETING] complete -- barge-in enabled")
            self._reset_listening()
            self._reset_interrupt_state()
            self._ignore_until = time.time() + ECHO_GRACE_S

        elif event == "clearedAudio":
            pass

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
                self._log_error("dialer", "media_frame", e, severity="warning")

    def _chunk_rms(self, data: bytes) -> float:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return 0.0
        return np.sqrt(np.mean(samples ** 2))

    def _chunk_ms(self, data: bytes) -> float:
        return (len(data) // 2 / PLIVO_SAMPLE_RATE) * 1000.0

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
        self._gnani_speech_end = False

    def _reset_interrupt_state(self):
        self._interrupt_ms = 0.0
        self._interrupt_frame_count = 0
        self._interrupt_voiced_count = 0
        self._interrupt_last_loud_time = None
        self._interrupt_seed = bytearray()

    def _estimate_pitch_hz(self, audio_bytes, sample_rate=PLIVO_SAMPLE_RATE):
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < 160:
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

    async def _write_positional(self, pcm_bytes: bytes, source: str):
        if not self.recording.get("start_time") or not pcm_bytes:
            return
        buf_key = "user_audio" if source == "user" else "bot_audio"
        async with self._audio_lock:
            elapsed = max(0, time.time() - self.recording["start_time"])
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
            return re.sub(r'[^A-Z0-9]+', '_', value).strip('_')
        phone_code = _clean(self.phone_number)
        parts = [p for p in ("PLIVO", phone_code) if p] or ["PLIVO_CALL", self.session_id[:8]]
        return "_".join(parts)

    async def save_conversation_recording(self):
        print(f"💾 [RECORD] Saving conversation for session {self.session_id}")
        user_bytes = bytes(self.recording["user_audio"])
        bot_bytes = bytes(self.recording["bot_audio"])
        if max(len(user_bytes), len(bot_bytes)) < 1000:
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

        stereo = np.empty(n * 2, dtype=np.int16)
        stereo[0::2] = left
        stereo[1::2] = right
        stereo_wav = os.path.join(temp_dir, f"{filename}_stereo_temp.wav")
        with wave.open(stereo_wav, 'wb') as wf:
            wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(self.RECORD_SAMPLE_RATE)
            wf.writeframes(stereo.tobytes())
        stereo_seg = AudioSegment.from_wav(stereo_wav)
        stereo_mp3 = os.path.join(temp_dir, f"{filename}_stereo.mp3")
        stereo_seg.export(stereo_mp3, format="mp3", bitrate="96k")

        mono = ((left.astype(np.int32) + right.astype(np.int32)) // 2).astype(np.int16)
        mono_wav = os.path.join(temp_dir, f"{filename}_mixed_temp.wav")
        with wave.open(mono_wav, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(self.RECORD_SAMPLE_RATE)
            wf.writeframes(mono.tobytes())
        mono_seg = AudioSegment.from_wav(mono_wav)
        mixed_mp3 = os.path.join(temp_dir, f"{filename}_mixed.mp3")
        mono_seg.export(mixed_mp3, format="mp3", bitrate="64k")

        try:
            await database_sync_to_async(_persist_recording_paths_sync)(
                self.session_id, stereo_mp3, mixed_mp3,
            )
        except Exception as e:
            self._log_error("db", "_persist_recording_paths_sync", e, severity="error")

        for p in (stereo_wav, mono_wav):
            if os.path.exists(p):
                os.remove(p)
        return stereo_mp3

    async def _process_plivo_audio(self, message: bytes):
        if (self.session["bot_speaking"]
                and time.time() > self.session.get("bot_speaking_until", 0) > 0):
            self.session["bot_speaking"] = False
            if self._greeting_active:
                self._greeting_active = False
                self._greeting_done = True
            self._reset_listening()
            self._reset_interrupt_state()
            self._ignore_until = time.time() + ECHO_GRACE_S

        if self._greeting_active and not self._greeting_done:
            return

        energy = self._chunk_rms(message)
        m_ms = self._chunk_ms(message)

        if self.session["bot_speaking"] or self.session["is_processing"]:
            if self._call_ending or not self._greeting_done:
                return
            if energy > INTERRUPT_THRESHOLD:
                now = time.time()
                is_voiced = self._estimate_pitch_hz(message) is not None
                self._interrupt_ms += m_ms
                self._interrupt_frame_count += 1
                if is_voiced:
                    self._interrupt_voiced_count += 1
                self._interrupt_last_loud_time = now
                self._interrupt_seed.extend(message)
                if len(self._interrupt_seed) > BYTES_PER_SEC:
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

        if self._stt_session:
            self._stt_session.feed(message)

        if energy > SPEECH_THRESHOLD:
            if not self._speech_started:
                self._speech_ms += m_ms
                if self._speech_ms >= SPEECH_MIN_MS:
                    self._speech_started = True
                    self._gnani_speech_end = False
                    self._stt_session.begin_turn()
                    asyncio.create_task(database_sync_to_async(set_speech_state)(self.session_id, "speaking"))
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

        gnani_ended = self._gnani_speech_end
        should_process = (
            self._speech_started
            and (gnani_ended or self._silence_ms >= SILENCE_END_MS)
            and len(self._audio_buffer) >= MIN_SPEECH_BYTES
        ) or len(self._audio_buffer) >= MAX_BUFFER

        if not should_process:
            return

        self.session["is_processing"] = True
        stt_session = self._stt_session
        utterance = bytes(self._audio_buffer)
        self._reset_listening()
        self._track(self._run_turn(utterance, stt_session))

    async def _run_turn(self, audio_bytes, stt_session):
        try:
            await self._process_plivo_utterance(audio_bytes, stt_session)
        finally:
            self.session["is_processing"] = False

    async def _do_interrupt(self, voice_ratio=0.0):
        print(f"🚨 [INTERRUPT] {self._interrupt_ms:.0f}ms voice_ratio={voice_ratio:.2f}")
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

        if seeded:
            self._speech_started = True
            self._speech_ms = SPEECH_MIN_MS
            self._silence_ms = 0.0
            self._audio_buffer.extend(seeded)
            try:
                self._stt_session.begin_turn()
                self._stt_session.feed(seeded)
            except Exception as e:
                self._log_error("stt", "barge_in_seed", e, severity="warning", seed_bytes=len(seeded))
            asyncio.create_task(database_sync_to_async(set_speech_state)(self.session_id, "speaking"))

    async def _tts_producer_queue(self, text, cancel_event):
        loop = asyncio.get_event_loop()
        q = asyncio.Queue()
        tts = get_tts_service()

        def producer():
            try:
                for chunk in tts.synthesize_stream(
                    text, provider=self._tts_provider, voice_name=self._tts_voice_name,
                ):
                    if cancel_event.is_set():
                        break
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                print(f"❌ [TTS] synthesize_stream failed: {e}")
                logger.error(f"[TTS] synthesize_stream failed: {e}")
                loop.call_soon_threadsafe(self._log_error, "tts", "synthesize_stream", e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(None, producer)
        return q

    async def _schedule_tts(self, text, is_first_sentence=False):
        cancel_event = threading.Event()
        queue = await self._tts_producer_queue(text, cancel_event)
        self._turn_tts_chars += len(text or "")
        item = {
            "text": text, "queue": queue, "cancel_event": cancel_event,
            "is_first_sentence": is_first_sentence, "is_filler": False,
            "start_time": time.time(),
        }
        async with self._turn_items_lock:
            self._turn_items.append(item)
        return item

    async def _plivo_send_pcm(self, piece: bytes):
        now = time.time()
        if self._playout_end < now:
            self._playout_end = now
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
                "contentType": "audio/x-l16", "sampleRate": PLIVO_SAMPLE_RATE,
                "payload": base64.b64encode(piece).decode(),
            },
        }))
        self._playout_end += len(piece) / BYTES_PER_SEC
        self.session["bot_speaking"] = True
        self.session["bot_speaking_until"] = self._playout_end + 0.5

    async def _audio_pump(self, timing_tracker: dict):
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
                        pcm, rate_state = audioop.ratecv(chunk, 2, 1, MURF_SAMPLE_RATE, PLIVO_SAMPLE_RATE, rate_state)
                    except Exception as e:
                        self._log_error("other", "resample_chunk", e, severity="warning", chunk_bytes=len(chunk))
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
                            if timing_tracker is not None and timing_tracker.get('tts_first_audio_ms') is None:
                                timing_tracker['tts_first_audio_ms'] = (time.time() - tts_start) * 1000
                            if item["is_first_sentence"] and timing_tracker.get('real_user_heard_at') is None:
                                timing_tracker['real_user_heard_at'] = time.time()
                            self._spoken_texts_this_turn.append(item["text"])
                            self._current_turn_partial_text = " ".join(self._spoken_texts_this_turn).strip()

                if out and not item["cancel_event"].is_set():
                    rem = len(out) % 320
                    if rem:
                        out.extend(b"\x00" * (320 - rem))
                    await self._plivo_send_pcm(bytes(out))
                    total_bytes += len(out)
                    sent_anything = True
                    if first:
                        self._spoken_texts_this_turn.append(item["text"])
                        self._current_turn_partial_text = " ".join(self._spoken_texts_this_turn).strip()

                idx += 1
        except StopAsyncIteration:
            pass
        finally:
            if timing_tracker is not None:
                timing_tracker['tts_total_bytes'] = total_bytes

        if sent_anything and not self._closed:
            name = f"m_{uuid.uuid4().hex[:6]}"
            self._last_checkpoint_name = name
            await self.safe_send(text_data=json.dumps({
                "event": "checkpoint", "streamId": self.session["stream_sid"], "name": name,
            }))

    async def _speak_standalone(self, text: str, tag="misc"):
        if not text or self._closed:
            return
        self._turn_items = []
        self._turn_items_done = False
        self._turn_tts_chars = 0
        timing = {}
        pump = self._track(self._audio_pump(timing))
        await self._schedule_tts(text, is_first_sentence=True)
        self._turn_items_done = True
        try:
            await pump
        except asyncio.CancelledError:
            raise
        finally:
            self._pending_tts_chars += self._turn_tts_chars
            self._turn_tts_chars = 0

    async def _process_plivo_utterance(self, audio_bytes: bytes, stt_session=None):
        timing = {
            'audio_received_at': time.time(), 'stt_done_at': None,
            'llm_first_token_at': None, 'llm_complete_at': None,
            'tts_first_audio_ms': None, 'tts_total_bytes': 0,
            'user_heard_at': None, 'real_user_heard_at': None,
        }
        pump_task = None
        self._turn_tts_chars = 0

        try:
            if stt_session is not None:
                speech_duration_estimate = len(audio_bytes) / BYTES_PER_SEC
                gnani_timeout = min(1.5, max(0.5, speech_duration_estimate + 0.4))
                try:
                    transcript = await stt_session.end_turn(rescue_audio=audio_bytes, timeout=gnani_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[STT] end_turn failed (session={self.session_id}): {e}")
                    self._log_error("stt", "end_turn", e, severity="error",
                                    backend=getattr(stt_session, "backend", None), audio_bytes=len(audio_bytes))
                    transcript = ""
            else:
                transcript = ""

            timing['stt_done_at'] = time.time()

            if not transcript:
                await self._handle_empty_transcript()
                return

            self._customer_text_history.append(transcript)
            print(f"📝 [STT] '{transcript}'")

            self.session["empty_count"] = 0
            self.session["last_transcript"] = transcript
            self.session["has_conversation"] = True

            self._current_turn_record = {
                "timestamp": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
                "user": transcript, "bot": "",
            }

            asyncio.create_task(database_sync_to_async(set_final_transcript)(self.session_id, transcript))
            history = await database_sync_to_async(get_history_for_llm)(self.session_id)
            await database_sync_to_async(save_conversation)(self.session_id, "Customer", transcript)
            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "customer", transcript,
                    stt_pricing=cost_stt_from_bytes(len(audio_bytes), sample_rate=PLIVO_SAMPLE_RATE),
                )
            )

            interrupted_context = self._interrupt_pending
            self._interrupt_pending = None
            self._current_turn_partial_text = ""
            self._spoken_texts_this_turn = []

            self._turn_items = []
            self._turn_items_done = False
            pump_task = self._track(self._audio_pump(timing))

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

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, True))

            async for chunk in self._llm_stream_async(
                self.session_id, transcript, cloud_context,
                history=history, interrupted_context=interrupted_context,
            ):
                if first_token_time is None:
                    first_token_time = time.time()
                    timing['llm_first_token_at'] = first_token_time

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
                    should_flush = (
                        (ends_sentence or ends_clause) and len(candidate) >= MIN_TTS_CHARS_FIRST
                    ) or len(candidate) >= MIN_TTS_CHARS_FIRST * 3
                else:
                    should_flush = ends_sentence and len(candidate) >= MIN_TTS_CHARS_AFTER_FIRST

                if should_flush:
                    text_buffer = ""
                    await self._schedule_tts(candidate, is_first_sentence=not first_sentence_sent)
                    first_sentence_sent = True

            if text_buffer.strip():
                await self._schedule_tts(text_buffer.strip(), is_first_sentence=not first_sentence_sent)
                first_sentence_sent = True

            if not full_response.strip():
                self._log_error("llm", "empty_response", "LLM stream produced no text",
                                severity="error", transcript=transcript[:200])

            timing['llm_complete_at'] = time.time()

            self._turn_items_done = True
            await pump_task
            pump_task = None

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, False))
            await database_sync_to_async(save_conversation)(self.session_id, self.persona_name, full_response)
            self._current_turn_partial_text = ""

            if self._current_turn_record is not None:
                self._current_turn_record["bot"] = full_response
                self.recording["transcript"].append(self._current_turn_record)
                self._current_turn_record = None

            timing['user_heard_at'] = time.time()
            turn_tts_cost = cost_tts(self._consume_tts_chars())

            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "bot", full_response,
                    timing=build_timing_record(timing), tts_pricing=turn_tts_cost,
                )
            )

            await self.safe_send(text_data=json.dumps({
                "event": "ai_response", "text": full_response, "back_flag": 2,
            }))

        except asyncio.CancelledError:
            async with self._turn_items_lock:
                for item in self._turn_items:
                    item["cancel_event"].set()
            self._turn_items_done = True

            spoken = (self._current_turn_partial_text or "").strip()
            if spoken:
                try:
                    await database_sync_to_async(save_conversation)(self.session_id, self.persona_name, spoken)
                    asyncio.create_task(
                        database_sync_to_async(save_turn)(
                            self.session_id, "bot", spoken, tts_pricing=cost_tts(self._consume_tts_chars()),
                        )
                    )
                    if self._current_turn_record is not None:
                        self._current_turn_record["bot"] = spoken
                        self.recording["transcript"].append(self._current_turn_record)
                        self._current_turn_record = None
                except Exception as save_err:
                    self._log_error("db", "save_turn(interrupted)", save_err, severity="warning")
            else:
                self._current_turn_record = None
            raise
        except Exception as e:
            print(f"❌ [Plivo] Error: {e}")
            import traceback
            traceback.print_exc()
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
                print(f"👋 [END-CALL] tool ended call (reason={end_call_signal.get('reason')})")
                wait_s = max(self.session.get("bot_speaking_until", 0) - time.time(), 0)
                await asyncio.sleep(min(wait_s, 10))
                try:
                    await self.close()
                except Exception as e:
                    print(f"⚠️ [END-CALL] close() raised: {e}")

    async def _handle_empty_transcript(self):
        self.session["empty_count"] += 1
        if self.session["empty_count"] >= 2:
            self.session["empty_count"] = 0
            await self._speak_standalone("जी, कृपया फिर से बोलिए...", tag="reprompt")

    async def _silence_watchdog(self):
        try:
            while not self._closed:
                await asyncio.sleep(SILENCE_WATCHDOG_TICK_S)
                if self._closed:
                    return
                if self.session.get("bot_speaking") or self.session.get("is_processing"):
                    self._listening_since = None
                    continue
                if not self._greeting_done:
                    continue
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
                    self._listening_since = None
                    self._track(self._handle_silence_checkin())
                elif self._silence_stage == 1 and idle_s >= SILENCE_DISCONNECT_S:
                    self._silence_stage = 2
                    self._track(self._handle_silence_disconnect())
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("other", "silence_watchdog", e, severity="warning")

    async def _handle_silence_checkin(self):
        import random
        text = random.choice(SILENCE_CHECKIN_TEXTS)
        try:
            await self._speak_standalone(text, tag="silence_checkin")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("tts", "silence_checkin", e, severity="warning")

    async def _handle_silence_disconnect(self):
        try:
            await self._speak_standalone(SILENCE_GOODBYE_TEXT, tag="silence_goodbye")
        except asyncio.CancelledError:
            raise
        except Exception as e:
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

    async def _llm_stream_async(self, session_id, customer_text, context,
                                 history=None, interrupted_context=None):
        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def producer():
            try:
                for chunk in chat_turn_stream(
                    session_id=session_id, customer_text=customer_text, context=context,
                    use_rag=True, history=history, interrupted_context=interrupted_context,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                print(f"❌ [LLM] chat_turn_stream failed: {e}")
                logger.error(f"[LLM] chat_turn_stream failed (session={session_id}): {e}")
                loop.call_soon_threadsafe(self._log_error, "llm", "chat_turn_stream", e)
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

    async def _send_greeting(self):
        ctx = self.session.get("cloud_context", {})
        customer_name = ctx.get("customer_name", "Customer")
        amount_due = ctx.get("amount_due")
        due_date = ctx.get("due_date")

        if customer_name and customer_name != "Customer":
            greeting = f"नमस्ते {customer_name} जी! मैं {self.persona_name} बोल रही हूँ।"
            if amount_due and amount_due not in ("0", 0, None):
                greeting += " आपकी पेमेंट के बारे में बात करनी थी"
                if due_date:
                    greeting += f", जिसकी due date {due_date} थी"
                greeting += "।"
        else:
            greeting = f"नमस्ते जी! मैं {self.persona_name} बोल रही हूँ।"

        print(f"🗣️ [GREETING] {greeting}")
        self._greeting_active = True
        await database_sync_to_async(save_conversation)(self.session_id, self.persona_name, greeting)
        try:
            await self._speak_standalone(greeting, tag="greeting")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[GREETING] failed (session={self.session_id}): {e}")
            self._log_error("tts", "greeting", e, severity="critical", greeting_chars=len(greeting))