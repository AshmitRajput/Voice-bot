"""
consumers.py — RecoverAI edition.

Fixed vs previous version:
    - Honda imports (Dealer, Branch, booking helpers, filler_service,
      filler_audio_cache) removed -- none of these exist in this project.
    - _resolve_dealer_branch_sync -> _resolve_customer_sync (views_admin.py).
    - _persist_recording_paths_sync call signature corrected to match the
      real function: (session_id, recording_stereo, recording_mixed) --
      no duration_seconds param.
    - _rag_ask_sync(dealer, module, branch, question, top_k) ->
      _rag_ask_sync(category, question, top_k) to match rag_service.py.
    - Entire STEP 1.4 booking block removed (Honda service-slot booking,
      no equivalent in a recovery agent).
    - Fast-path intent shortcuts (classify_intent, _end_call_turn,
      _callback_turn) removed per decision -- every turn now goes through
      the LLM + tool-calling loop in cloud_llm_service.chat_turn_stream /
      client.py, which already executes end_call/schedule_callback via
      tool_registry and fires _on_end_call_signal correctly. This was
      already wired up; removing the shortcut just makes it the only path.
    - Greeting and all transcript/history labels use the dynamic persona
      name from views.get_active_persona_config() instead of hardcoded
      "Aarohi"/Honda dealer text.
    - cloud_context now carries amount_due/due_date/recovery_status/
      call_attempt_number/promise_broken/persona_config instead of
      vehicle_model/branch/module.
    - TTS voice_name now comes from the active LLMSetting.voice row
      instead of being hardcoded.
"""
import json
import base64
import asyncio
import time
import audioop
import uuid
import logging
import threading
import datetime
from decimal import Decimal, ROUND_HALF_UP
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import numpy as np
from .services.cloud_llm_service import (
    chat_turn_stream, build_timing_record, score_and_price_turn, get_last_turn_usage,
)
from .services.tts_service import get_tts_service
import wave
import os
from django.conf import settings
from .services.rag_service import get_rag_service
from .services.stt_service import STTSession
from .services.conversation_history import (
    init_state, set_speech_state, set_final_transcript, set_generating, clear_state,
    save_conversation,
)
from .views import (
    get_or_create_call_session, get_customer_context, get_customer_context_by_phone,
    get_random_customer_context, save_turn, end_call_session, finalize_call_summary,
    get_history_for_llm, save_turn_scores, log_service_error,
    get_active_persona_config, get_active_llm_setting_raw,
)
from .views_admin import _get_barge_in_settings_sync, _resolve_customer_sync, _persist_recording_paths_sync
from .tools.tool_registry import (
    should_end_call, register_end_call_handler, unregister_end_call_handler,
    set_call_context, clear_call_context,
)
from .models import LLMSetting, Customer, RecoveryCase
from urllib.parse import parse_qs
from django.utils import timezone
import re
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

DEBUG_VAD_RMS = False

logger = logging.getLogger('recovery_agent')


# ═══════════════════════════════════════════════════════════════
# COST CALCULATION (unchanged provider math)
# ═══════════════════════════════════════════════════════════════
STT_PER_HOUR = Decimal('27')
USD_TO_INR = Decimal('88')
TTS_USD_PER_1K = Decimal('0.01')
TTS_PER_1K_CHARS = TTS_USD_PER_1K * USD_TO_INR

_COST_Q = Decimal('0.000001')


def _quantize_cost(value):
    return Decimal(value).quantize(_COST_Q, rounding=ROUND_HALF_UP)


def cost_stt_from_bytes(audio_bytes_len, sample_rate=16000):
    if not audio_bytes_len:
        return Decimal('0')
    seconds = Decimal(int(audio_bytes_len)) / (sample_rate * 2)
    return _quantize_cost(seconds / 3600 * STT_PER_HOUR)


def cost_tts(chars):
    if not chars:
        return Decimal('0')
    return _quantize_cost(Decimal(int(chars)) / 1000 * TTS_PER_1K_CHARS)


_LEAK_PATTERNS = re.compile(
    r'(TOOL\s*CALL\s*:|function_call|\{"tool"\s*:|\{\s*"response_text)',
    re.IGNORECASE,
)


def _now_ist():
    return timezone.now().astimezone(IST)


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


BARGE_IN_THRESHOLD_MIN_RMS = 700
BARGE_IN_THRESHOLD_MAX_RMS = 2200
BARGE_IN_DEFAULT_ENABLED = True
BARGE_IN_DEFAULT_RMS = 900


def _rag_ask_sync(category, question, top_k=3):
    """Sync wrapper so consumers.py can call it via asyncio.to_thread.
    Fails soft (empty result) instead of raising into the hot path."""
    try:
        return get_rag_service().ask_question(category=category, question=question, top_k=top_k)
    except Exception as e:
        logger.error(f"[RAG] ask_question failed: {e}")
        return {"success": False, "error": str(e), "contexts": [], "sources": [], "best_distance": None}


class VoiceChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket: Browser -> STT -> Cloud LLM (BharatRouter + RAG + tool-calling
    recovery agent) -> Murf TTS Streaming.

    This is the browser/demo path. Plivo dialer integration (outbound
    calling driven from the admin dashboard + campaign data, with the same
    Redis state / conversation history / DB tool-calling flow) is a
    separate consumer (dialer.py) that will reuse this file's service
    layer (cloud_llm_service, recovery_service, tool_registry) unchanged.
    """

    BOT_AUDIO_SAMPLE_RATE = 24000
    PLAYBACK_LEAD_MS = 200
    PACED_SEND_LEAD_S = 0.6

    def _log_error(self, provider, stage, exc, severity='error', **context):
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
            logger.warning(f"[ERRLOG] could not schedule error log ({provider}/{stage}): {e}")

    def _on_end_call_signal(self, payload):
        """Fires synchronously the instant the end_call tool executes
        mid-LLM-stream (see tool_registry.mark_call_for_ending, called from
        the tool impl in call_control_tools_defs.py). This is now the ONLY
        path that ends a call -- there is no separate fast-path intent
        shortcut anymore."""
        self._call_ending = True
        print(f"🔒 [END-CALL] signalled (reason={payload.get('reason')}) -- "
              f"this turn is now barge-in-immune")

    async def connect(self):
        await self.accept()
        self._closed = False
        self._active_tasks = set()
        self._barge_in_fired = False
        self._bot_speaking_clear_task = None
        self._playout_end = 0.0
        self._noise_floor = 300.0
        self._adaptive_threshold = self.VOICE_RMS_THRESHOLD
        self._gnani_speech_end = False
        self._turn_start_time = None
        self._barge_in_frame_count = 0
        self._barge_in_voiced_frame_count = 0
        self._barge_in_cooldown_until = 0.0

        self._barge_in_enabled = BARGE_IN_DEFAULT_ENABLED
        self._barge_in_rms_threshold = BARGE_IN_DEFAULT_RMS

        self._bot_play_position = 0
        self._bot_play_anchor_time = None
        self._bot_play_anchor_offset = 0

        self.session_id = str(uuid.uuid4())

        self._call_ending = False
        register_end_call_handler(self.session_id, asyncio.get_event_loop(), self._on_end_call_signal)

        query_params = parse_qs((self.scope.get("query_string") or b"").decode())
        self.phone_number = (query_params.get("phone") or [None])[0]

        self.session = {
            "history": [],
            "is_processing": False,
            "bot_speaking": False,
            "has_conversation": False,
        }
        self._write_cursor = {"user": 0, "bot": 0}

        self.recording = {
            "active": True,
            "user_audio": bytearray(),
            "bot_audio": bytearray(),
            "start_time": time.time(),
            "transcript": [],
        }

        self._audio_lock = asyncio.Lock()
        self.RECORD_SAMPLE_RATE = 16000
        self._stt_session = None
        self._barge_in_buffer = bytearray()
        self._barge_in_voice_start = None
        self._tts_send_lock = asyncio.Lock()
        self._current_process_task = None
        self._turn_items = []
        self._turn_items_lock = asyncio.Lock()
        self._turn_items_done = False

        self._bot_write_queue = asyncio.Queue()
        self._bot_write_task = asyncio.create_task(self._bot_write_worker())

        self._session_prompt_tokens = 0
        self._session_output_tokens = 0

        self._turn_tts_chars = 0
        self._greeting_tts_chars = 0

        self._score_tasks = []
        self._intent_history = []
        self._customer_text_history = []
        self._bot_resample_state = None

        asyncio.create_task(database_sync_to_async(init_state)(self.session_id))

        # ── Persona: load once per call, from the active LLMSetting row.
        # Falls back to None -> client.py/prompt_builder.py's built-in
        # Riya default if no admin row is configured yet.
        llm_setting = await database_sync_to_async(get_active_llm_setting_raw)()
        self.persona_config = await database_sync_to_async(get_active_persona_config)()
        self.persona_name = (self.persona_config or {}).get("name") or "Riya"
        self._tts_voice_name = (
            getattr(llm_setting.voice, "provider_voice_id", None)
            if llm_setting and llm_setting.voice else None
        ) or "hi-IN-sunaina"
        self._tts_provider = getattr(llm_setting, "provider", None) if llm_setting else None

        # ── Customer / recovery-case context
        if self.phone_number:
            context = await database_sync_to_async(get_customer_context_by_phone)(self.phone_number)
            if context is None:
                logger.warning(
                    f"[WS] no Customer found for phone={self.phone_number} -- "
                    f"falling back to a random seeded customer's context for this demo call."
                )
                context = await database_sync_to_async(get_random_customer_context)()
        else:
            context = await database_sync_to_async(get_random_customer_context)()

        self.customer_id = context.get("customer_id")
        effective_phone = context.get("phone_number") or self.phone_number
        self._effective_phone = effective_phone

        self._call_session_created = False
        self._call_session_lock = asyncio.Lock()
        self._call_session_task = None

        self.customer = await database_sync_to_async(_resolve_customer_sync)(
            phone_number=effective_phone, customer_id=self.customer_id,
        )

        # call_attempt_number / promise_broken: how many prior calls this
        # customer has had, and whether an existing promise_date has passed
        # without being paid. Cheap to compute here, read once per call.
        call_attempt_number, promise_broken = await database_sync_to_async(
            self._resolve_escalation_state
        )(self.customer_id)

        self.session["cloud_context"] = {
            "customer_id": self.customer_id,
            "customer_name": context.get("customer_name", "Customer"),
            "amount_due": context.get("amount_due", "0"),
            "outstanding_amount": context.get("amount_due", "0"),
            "due_date": context.get("due_date"),
            "recovery_status": context.get("recovery_status", "no_open_case"),
            "call_attempt_number": call_attempt_number,
            "promise_broken": promise_broken,
            "workflow": "revenue_recovery",
            "persona_config": self.persona_config,
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

        self._stt_session = STTSession(sample_rate=16000)
        self._stt_session.on_speech_end = self._on_speech_end_signal
        self._stt_connect_task = self._track(self._stt_session.connect())

        await self.send_greeting()

    @staticmethod
    def _resolve_escalation_state(customer_id):
        """Returns (call_attempt_number, promise_broken) for the LLM's
        escalation-tier rules. call_attempt_number = completed calls so far
        + 1 (this call). promise_broken = an open case has a promise_date
        in the past that was never fulfilled (case still not closed)."""
        if not customer_id:
            return 1, False
        try:
            from .models import CallSession
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

    async def disconnect(self, close_code):
        print(f"🔌 [WS] Disconnected: {close_code}")
        unregister_end_call_handler(self.session_id)
        clear_call_context(self.session_id)
        if self.session.get("has_conversation") and self.recording.get("active"):
            try:
                if getattr(self, "_call_session_task", None):
                    await self._call_session_task
                await self.save_conversation_recording()
            except Exception as e:
                print(f"❌ [RECORD] Failed to save: {e}")
                self._log_error("recording", "save_conversation_recording", e,
                                severity="error", close_code=close_code)
        self._closed = True
        pending = [t for t in self._active_tasks if not t.done()]
        print(f"🔌 [WS] Disconnected: {close_code}, cancelling {len(pending)} pending task(s)")
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
                    self.session_id, self.session.get("cloud_context"),
                    total_prompt_tokens=self._session_prompt_tokens,
                    total_output_tokens=self._session_output_tokens,
                ),
                label="finalize_call_summary",
            )

    def _track(self, coro):
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _ensure_call_session(self):
        if self._call_session_created:
            return
        async with self._call_session_lock:
            if self._call_session_created:
                return
            self._call_session_created = True
            self._call_session_task = asyncio.create_task(
                database_sync_to_async(get_or_create_call_session)(
                    self.session_id, phone_number=self._effective_phone, customer_id=self.customer_id,
                )
            )

    def _on_speech_end_signal(self):
        if self._speech_started:
            self._gnani_speech_end = True

    def _reset_barge_in_state(self):
        self._barge_in_buffer = bytearray()
        self._barge_in_voice_start = None
        self._barge_in_frame_count = 0
        self._barge_in_voiced_frame_count = 0
        self._barge_in_last_loud_time = None
        self._noise_floor = 300.0
        self._adaptive_threshold = self.VOICE_RMS_THRESHOLD

    def _reset_playout_state(self):
        self._playout_end = 0.0

    def _cancel_bot_speaking_fallback(self):
        task = getattr(self, "_bot_speaking_clear_task", None)
        if task and not task.done():
            task.cancel()

    async def _clear_bot_speaking_after_delay(self, delay, tag):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self.session.get("bot_speaking"):
            print(f"⚠️ [BOT] playback_end never arrived for {tag} "
                  f"(waited {delay:.2f}s) -- clearing bot_speaking via fallback timer.")
            logger.warning("[BOT] playback_end never arrived for %s (waited %.2fs)", tag, delay)
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

        write_at = time.time() + self.PACED_SEND_LEAD_S
        await self._bot_write_queue.put((chunk, write_at))

        self._playout_end += len(chunk) / (self.BOT_AUDIO_SAMPLE_RATE * 2)

    async def _bot_write_worker(self):
        while True:
            try:
                item = await self._bot_write_queue.get()
            except asyncio.CancelledError:
                return
            if item is None:
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
                self._log_error("recording", "bot_write_worker", e,
                                severity="warning", chunk_bytes=len(chunk) if chunk else 0)

    def _drain_bot_write_queue(self):
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
        self._cancel_bot_speaking_fallback()
        remaining = self._playout_end - time.time()
        if remaining <= 0:
            remaining = total_bytes / (sample_rate * 2) if sample_rate else 0.0
        delay = max(min_delay, remaining) + safety_buffer
        self._bot_speaking_clear_task = self._track(
            self._clear_bot_speaking_after_delay(delay, tag)
        )

    async def _cancel_current_turn(self):
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
            self._gnani_speech_end = False
            self._track(self.send_greeting())

        elif msg_type == "playback_start":
            self.session["bot_speaking"] = True
            self._turn_start_time = time.time()
            print("🔊 [BOT] speaking")

        elif msg_type == "playback_end":
            self._cancel_bot_speaking_fallback()
            self.session["bot_speaking"] = False
            self._reset_barge_in_state()
            self._reset_playout_state()
            print("👂 [VAD] listening")

        elif msg_type == "interrupt":
            print("🚨 [INTERRUPT] user interrupted")
            interrupt_detected_at = time.time()
            self._cancel_bot_speaking_fallback()
            await self._cancel_current_turn()
            self._drain_bot_write_queue()
            await self._trim_recording_to_actual_playback(cutoff_time=interrupt_detected_at)
            self.session["bot_speaking"] = False
            self.session["is_processing"] = False
            self._reset_barge_in_state()
            self._reset_playout_state()
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "bot_interrupted"}))

    VOICE_RMS_THRESHOLD = 300
    BARGE_IN_RMS_THRESHOLD = 900
    BARGE_IN_SUSTAIN_DURATION = 0.5
    BARGE_IN_FAST_MULTIPLIER = 2.2
    BARGE_IN_FAST_SUSTAIN = 0.2
    BARGE_IN_MIN_FRAMES = 2
    BARGE_IN_MIN_TURN_AGE = 0.2
    BARGE_IN_COOLDOWN = 0.5

    BARGE_IN_PITCH_MIN_HZ = 100
    BARGE_IN_PITCH_MAX_HZ = 300
    BARGE_IN_PITCH_PERIODICITY_MIN = 0.30
    BARGE_IN_VOICE_RATIO_THRESHOLD = 0.40
    BARGE_IN_MAX_WINDOW_SECONDS = 1.5

    PREROLL_MS = 500
    PREROLL_BYTES = 16000

    def _estimate_pitch_hz(self, audio_bytes, sample_rate=16000):
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < 320:
            return None
        samples = samples - np.mean(samples)
        if np.max(np.abs(samples)) < 1e-6:
            return None
        corr = np.correlate(samples, samples, mode='full')
        corr = corr[len(corr) // 2:]
        if corr[0] <= 0:
            return None
        min_lag = int(sample_rate / self.BARGE_IN_PITCH_MAX_HZ)
        max_lag = int(sample_rate / self.BARGE_IN_PITCH_MIN_HZ)
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

        if rms > threshold:
            sustain_needed = (
                self.BARGE_IN_FAST_SUSTAIN
                if rms > threshold * self.BARGE_IN_FAST_MULTIPLIER
                else self.BARGE_IN_SUSTAIN_DURATION
            )
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
                    self._barge_in_frame_count >= self.BARGE_IN_MIN_FRAMES
                    and voice_ratio >= self.BARGE_IN_VOICE_RATIO_THRESHOLD
                ):
                    buffered = bytes(self._barge_in_buffer)
                    self._reset_barge_in_state()
                    await self._trigger_barge_in(buffered)
                elif now - self._barge_in_voice_start >= self.BARGE_IN_MAX_WINDOW_SECONDS:
                    self._reset_barge_in_state()
        else:
            if self._barge_in_voice_start is not None:
                if not hasattr(self, '_barge_in_last_loud_time'):
                    self._barge_in_last_loud_time = self._barge_in_voice_start
                if now - self._barge_in_last_loud_time > 0.25:
                    self._reset_barge_in_state()
            else:
                self._reset_barge_in_state()

    async def _trigger_barge_in(self, buffered_audio):
        self._barge_in_fired = True
        barge_in_detected_at = time.time()
        self._barge_in_cooldown_until = barge_in_detected_at + self.BARGE_IN_COOLDOWN
        try:
            print("🚨 [BARGE-IN] sustained user voice over bot speech -- cancelling turn")
            self._cancel_bot_speaking_fallback()
            await self._cancel_current_turn()
            self._drain_bot_write_queue()
            await self._trim_recording_to_actual_playback(cutoff_time=barge_in_detected_at)
            self.session["bot_speaking"] = False
            self.session["is_processing"] = False
            self._reset_playout_state()
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "bot_interrupted"}))

            self._audio_buffer = bytearray(buffered_audio)
            self._speech_started = True
            self._speech_start_time = time.time() - self.BARGE_IN_SUSTAIN_DURATION
            self._last_voice_time = time.time()

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
        if not self.recording["active"]:
            self.recording["active"] = True
            self.recording["start_time"] = time.time()
            print(f"🔴 [RECORD] Started recording for session {self.session_id}")

        await self._write_positional(audio_bytes, source_sample_rate=16000, source="user")

        if self.session["bot_speaking"]:
            await self._handle_barge_in_audio(audio_bytes, threshold=self._barge_in_rms_threshold)
            return

        if self.session["is_processing"]:
            await self._handle_barge_in_audio(audio_bytes, threshold=self._adaptive_threshold)
            return

        if not hasattr(self, '_audio_buffer'):
            self._audio_buffer = bytearray()
            self._last_voice_time = None
            self._speech_started = False

        self._audio_buffer.extend(audio_bytes)

        if not self._speech_started and len(self._audio_buffer) > self.PREROLL_BYTES:
            del self._audio_buffer[:len(self._audio_buffer) - self.PREROLL_BYTES]

        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples ** 2)) if len(samples) else 0.0

        if not self._speech_started and rms < self._adaptive_threshold * 2:
            alpha = 0.02
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * rms
            self._adaptive_threshold = max(200.0, self._noise_floor * 3.0)

        if DEBUG_VAD_RMS:
            print(f"🔍 [VAD-DEBUG] rms={rms:.0f} adaptive_threshold={self._adaptive_threshold:.0f} "
                  f"noise_floor={self._noise_floor:.0f} speech_started={self._speech_started} "
                  f"len_bytes={len(audio_bytes)}")

        SILENCE_HANGOVER = 0.6
        MIN_SPEECH_DURATION = 0.15
        MIN_BUFFER_BYTES = 8000

        now = time.time()

        if self._stt_session:
            self._stt_session.feed(audio_bytes)

        if rms > self._adaptive_threshold:
            if not self._speech_started:
                self._speech_start_time = now
                self._speech_started = True
                print("🎙️ [VAD] speech start")
                self._stt_session.begin_turn()
                print(f"🎙️ [STT] backend={self._stt_session.backend}")
                asyncio.create_task(
                    database_sync_to_async(set_speech_state)(self.session_id, "speaking")
                )
            self._last_voice_time = now

        if self._speech_started and self._last_voice_time:
            silence_duration = now - self._last_voice_time
            speech_duration = self._last_voice_time - self._speech_start_time
            gnani_ended = self._gnani_speech_end
            if (
                (gnani_ended or silence_duration > SILENCE_HANGOVER)
                    and speech_duration >= MIN_SPEECH_DURATION
                    and len(self._audio_buffer) >= MIN_BUFFER_BYTES):
                if gnani_ended:
                    print(f"⚡ [VAD] speech-end signal — early dispatch "
                          f"(local silence was only {silence_duration:.0f}ms)")
                stt_session = self._stt_session
                utterance_bytes = bytes(self._audio_buffer)
                self._audio_buffer = bytearray()
                self._speech_started = False
                self._last_voice_time = None
                self._gnani_speech_end = False

                await self._cancel_current_turn()
                self._current_process_task = self._track(
                    self.process_utterance(utterance_bytes, stt_session)
                )

    def _advance_bot_play_cursor(self, now: float):
        bytes_per_sec = self.RECORD_SAMPLE_RATE * 2
        write_cursor = self._write_cursor.get("bot", 0)

        if self._bot_play_anchor_time is None:
            self._bot_play_anchor_time = now
            self._bot_play_anchor_offset = write_cursor
            self._bot_play_position = write_cursor
            return

        elapsed = max(0.0, now - self._bot_play_anchor_time)
        theoretical = self._bot_play_anchor_offset + int(elapsed * bytes_per_sec)
        position = min(theoretical, write_cursor)
        self._bot_play_position = position

        if position >= write_cursor:
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

            buf[offset:offset + len(pcm_bytes)] = pcm_bytes
            self._write_cursor[source] = offset + len(pcm_bytes)

    async def _trim_recording_to_actual_playback(self, cutoff_time=None):
        now = cutoff_time if cutoff_time is not None else time.time()
        lead_bytes = int((self.PLAYBACK_LEAD_MS / 1000.0) * self.RECORD_SAMPLE_RATE * 2)

        async with self._audio_lock:
            self._advance_bot_play_cursor(now)
            write_cursor = self._write_cursor.get("bot", 0)
            cutoff = max(0, self._bot_play_position - lead_bytes)

            if cutoff < write_cursor:
                trimmed = write_cursor - cutoff
                self.recording["bot_audio"][cutoff:write_cursor] = b'\x00' * trimmed
                self._write_cursor["bot"] = cutoff
                print(f"✂️ [RECORD] Trimmed {trimmed} bytes of unheard bot audio (barge-in)")
                self._bot_play_position = cutoff
                self._bot_play_anchor_time = now
                self._bot_play_anchor_offset = cutoff

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
            "queue": queue,
            "cancel_event": cancel_event,
            "is_first_sentence": is_first_sentence,
            "is_filler": False,
            "start_time": time.time(),
        }
        async with self._turn_items_lock:
            self._turn_items.append(item)
        return item

    def _consume_tts_chars(self):
        total = self._turn_tts_chars + self._greeting_tts_chars
        self._turn_tts_chars = 0
        self._greeting_tts_chars = 0
        return total

    async def _audio_pump(self, timing_tracker):
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
                        first = False
                    await self._send_bot_pcm(chunk)
            idx += 1

    async def _llm_stream_async(self, session_id, customer_text, context, history=None):
        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def producer():
            try:
                for chunk in chat_turn_stream(
                    session_id=session_id, customer_text=customer_text,
                    context=context, use_rag=False,
                    history=history or [],
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

    async def _score_and_persist_turn(self, session_id, customer_text, bot_response_text,
                                       turn_prompt_tokens, turn_output_tokens, bot_turn_task):
        try:
            bot_turn = await bot_turn_task
        except Exception as e:
            logger.error(f"[SCORE] save_turn task failed (session={session_id}): {e}")
            self._log_error("db", "save_turn(bot)", e, severity="error")
            return
        if not bot_turn:
            logger.warning(f"[SCORE] save_turn returned no row (session={session_id})")
            return

        try:
            turn_scores = await asyncio.to_thread(
                score_and_price_turn, customer_text, bot_response_text, None,
                turn_prompt_tokens, turn_output_tokens,
            )
        except Exception as e:
            logger.error(f"[SCORE] score_and_price_turn failed (session={session_id}): {e}")
            self._log_error("llm", "score_and_price_turn", e, severity="warning", turn_id=bot_turn.id)
            return

        try:
            await database_sync_to_async(save_turn_scores)(
                bot_turn.id,
                accuracy=turn_scores.get("accuracy"),
                llm_pricing=turn_scores.get("llm_pricing"),
            )
        except Exception as e:
            logger.error(f"[SCORE] save_turn_scores failed (session={session_id}, turn_id={bot_turn.id}): {e}")
            self._log_error("db", "save_turn_scores", e, severity="error", turn_id=bot_turn.id)

    async def process_utterance(self, audio_bytes, stt_session=None):
        """Streaming-STT -> Cloud LLM (with tool-calling: get_recovery_context,
        update_recovery_case, create_payment_link, schedule_callback,
        end_call) -> TTS Streaming. Every turn goes through this single
        path -- there is no fast-path intent shortcut anymore; the LLM
        decides tone/pressure/closing based on the escalation context
        assembled in connect(), and end_call/schedule_callback (both
        terminal tools) are what actually end the call, via
        _on_end_call_signal / should_end_call() below."""
        self.session["is_processing"] = True
        self._turn_tts_chars = 0

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
            print(f"\n{'='*60}\n🎤 [TURN START] New user turn\n{'='*60}")
            pump_task = None

            if stt_session is not None:
                speech_duration_estimate = len(audio_bytes) / (16000 * 2)
                gnani_timeout = min(1.5, max(0.5, speech_duration_estimate + 0.4))
                try:
                    transcript = await stt_session.end_turn(rescue_audio=audio_bytes, timeout=gnani_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
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
                await self.safe_send(text_data=json.dumps({"type": "no_speech", "message": "no transcript detected"}))
                await self.safe_send(text_data=json.dumps({"type": "done"}))
                return

            print(f"📝 [STT] '{transcript}'")
            print(f"⏱️ [STT] Audio → Text: {stt_latency:.0f}ms")

            self.session["has_conversation"] = True

            turn_record = {
                "timestamp": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
                "user": transcript,
                "bot": "",
            }

            asyncio.create_task(database_sync_to_async(set_final_transcript)(self.session_id, transcript))
            asyncio.create_task(
                database_sync_to_async(save_turn)(
                    self.session_id, "customer", transcript,
                    stt_pricing=cost_stt_from_bytes(len(audio_bytes)),
                )
            )
            llm_history = await database_sync_to_async(get_history_for_llm)(self.session_id)
            await database_sync_to_async(save_conversation)(self.session_id, "Customer", transcript)

            await self.safe_send(text_data=json.dumps({"type": "transcript", "text": transcript}))
            await self.safe_send(text_data=json.dumps({"type": "pcm_start"}))
            self.session["bot_speaking"] = True
            self._turn_start_time = time.time()

            # ── RAG: intent-agnostic now (no filler_service classifier) --
            # a lightweight always-on lookup against the communication
            # policy category, capped by timeout, fail-open.
            rag_context = None
            RAG_SIMILARITY_MAX_DISTANCE = 0.45
            try:
                rag_task = asyncio.create_task(
                    asyncio.to_thread(_rag_ask_sync, None, transcript, 3)
                )
                rag_result = await asyncio.wait_for(rag_task, timeout=0.35)
            except asyncio.TimeoutError:
                rag_result = None
                print("⏱️ [RAG] timed out, proceeding without context")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                rag_result = None
                print(f"❌ [RAG] ask_question failed: {e}")
                logger.error(f"[RAG] ask_question failed (session={self.session_id}): {e}")
                self._log_error("rag", "ask_question", e, severity="warning")

            if rag_result and rag_result.get("success"):
                contexts = rag_result.get("contexts", [])
                best_distance = rag_result.get("best_distance")
                if contexts and best_distance is not None and best_distance <= RAG_SIMILARITY_MAX_DISTANCE:
                    rag_context = "\n".join(contexts)
                    print(f"📚 [RAG] using context (distance={best_distance:.3f})")
                else:
                    print(f"📚 [RAG] no close-enough match (distance={best_distance}), skipping")

            base_context = self.session.get("cloud_context") or {}
            cloud_context = {
                **base_context,
                "today": timezone.now().date().strftime("%Y-%m-%d (%A)"),
                "current_datetime_ist": _now_ist().strftime("%Y-%m-%d %H:%M"),
                "customer_history_summary": self._customer_text_history,
            }
            self._customer_text_history.append(transcript)

            full_response = ""
            first_token_time = None
            llm_start_time = time.time()

            print(f"\n🤖 [LLM] Starting stream...")
            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, True))

            self._turn_items = []
            self._turn_items_done = False
            pump_task = self._track(self._audio_pump(timing))

            text_buffer = ""
            first_sentence_sent = False
            MIN_TTS_CHARS_FIRST = 25
            MIN_TTS_CHARS_AFTER_FIRST = 60

            async for chunk in self._llm_stream_async(
                self.session_id, transcript, cloud_context, history=llm_history,
            ):
                if first_token_time is None:
                    first_token_time = time.time()
                    timing['llm_first_token_at'] = first_token_time
                    ttft = (first_token_time - llm_start_time) * 1000
                    print(f"⚡ [LLM] First token: {ttft:.0f}ms")

                full_response += chunk
                text_buffer += chunk
                if _LEAK_PATTERNS.search(text_buffer):
                    print(f"⚠️ [LLM] Leaked tool-call/JSON text detected, suppressing: {text_buffer!r}")
                    logger.warning("Leaked non-speech text from LLM | text=%r", text_buffer)
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

                if not first_sentence_sent:
                    should_flush = (
                        (ends_sentence or ends_clause) and len(candidate) >= MIN_TTS_CHARS_FIRST
                    ) or len(candidate) >= MIN_TTS_CHARS_FIRST * 3
                else:
                    should_flush = ends_sentence and len(candidate) >= MIN_TTS_CHARS_AFTER_FIRST

                if should_flush:
                    sentence = candidate
                    text_buffer = ""
                    print(f"\n🔥 [TTS] Chunk ready: {sentence[:50]}...")
                    await self._schedule_tts(sentence, is_first_sentence=not first_sentence_sent)
                    first_sentence_sent = True

            if text_buffer.strip():
                print(f"\n🔥 [TTS] Final buffer: {text_buffer[:50]}...")
                await self._schedule_tts(text_buffer.strip(), is_first_sentence=not first_sentence_sent)
                first_sentence_sent = True

            if not full_response.strip():
                self._log_error("llm", "empty_response", "LLM stream produced no text this turn",
                                severity="error", transcript=transcript[:200])

            turn_usage = get_last_turn_usage()
            turn_prompt_tokens = turn_usage.get("prompt_tokens") or 0
            turn_output_tokens = turn_usage.get("output_tokens") or 0
            self._session_prompt_tokens += turn_prompt_tokens
            self._session_output_tokens += turn_output_tokens

            self._turn_items_done = True
            await pump_task
            await database_sync_to_async(save_conversation)(self.session_id, self.persona_name, full_response)

            turn_record["bot"] = full_response
            self.recording["transcript"].append(turn_record)
            print(f"📝 [TRANSCRIPT] Turn saved: User='{transcript[:30]}...' Bot='{full_response[:30]}...'")

            asyncio.create_task(database_sync_to_async(set_generating)(self.session_id, False))

            timing['llm_complete_at'] = time.time()
            llm_total = (timing['llm_complete_at'] - llm_start_time) * 1000
            print(f"\n⏱️ [LLM] Total LLM time: {llm_total:.0f}ms")

            timing['user_heard_at'] = time.time()

            turn_tts_cost = cost_tts(self._consume_tts_chars())

            bot_turn_task = self._track(
                database_sync_to_async(save_turn)(
                    self.session_id, "bot", full_response,
                    timing=build_timing_record(timing),
                    tts_pricing=turn_tts_cost,
                )
            )

            self._score_tasks.append(
                _fire_and_forget(
                    self._score_and_persist_turn(
                        self.session_id, transcript, full_response,
                        turn_prompt_tokens, turn_output_tokens, bot_turn_task,
                    ),
                    label="score_turn",
                )
            )

            total_turn = (timing['user_heard_at'] - timing['audio_received_at']) * 1000
            print(f"⏱️  TOTAL TURN TIME (Audio → Heard): {total_turn:>8.0f}ms")

            await self.safe_send(text_data=json.dumps({
                "type": "ai_response", "text": full_response, "back_flag": 2, "usage": "0",
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
            print(f"❌ [WS] process_utterance crashed: {e}")
            logger.exception(f"[WS] process_utterance crashed (session={self.session_id}): {e}")
            self._log_error("other", "process_utterance", e, severity="critical")
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
                print(f"👋 [END-CALL] tool call ended session "
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

    async def do_stt(self, audio_bytes):
        """Fallback path if no persistent stt_session is available -- not
        normally hit, since connect() always opens one."""
        try:
            from .services.stt_service import GoogleSTTSession
            session = GoogleSTTSession(sample_rate=16000)
            session.start()
            session.feed(audio_bytes)
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, session.finish)
        except Exception as e:
            print(f"❌ [STT] Error: {e}")
            self._log_error("stt", "do_stt_fallback", e, severity="error",
                            audio_bytes=len(audio_bytes) if audio_bytes else 0)
            return ""

    async def send_greeting(self):
        """Dynamic greeting: persona name + recovery framing (amount_due,
        due_date), not Honda dealer/vehicle text."""
        context = self.session.get("cloud_context") or {}
        customer_name = context.get("customer_name", "Customer")
        amount_due = context.get("amount_due")
        due_date = context.get("due_date")

        if customer_name and customer_name != "Customer":
            greeting = f"नमस्ते {customer_name} जी! मैं {self.persona_name} बोल रही हूँ।"
            if amount_due and amount_due not in ("0", 0, None):
                greeting += f" आपकी पेमेंट के बारे में बात करनी थी"
                if due_date:
                    greeting += f", जिसकी due date {due_date} थी"
                greeting += "।"
        else:
            greeting = f"नमस्ते जी! मैं {self.persona_name} बोल रही हूँ।"

        if self._closed:
            return

        await database_sync_to_async(save_conversation)(self.session_id, self.persona_name, greeting)

        await self.safe_send(text_data=json.dumps({"type": "transcript", "text": greeting}))

        tts = get_tts_service()
        try:
            audio_stream = await database_sync_to_async(tts.synthesize_stream)(
                greeting, provider=self._tts_provider, voice_name=self._tts_voice_name,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ [TTS] greeting synthesis failed: {e}")
            logger.error(f"[TTS] greeting synthesis failed (session={self.session_id}): {e}")
            self._log_error("tts", "greeting_synthesize_stream", e, severity="critical",
                            greeting_chars=len(greeting))
            await self.safe_send(text_data=json.dumps({"type": "pcm_end"}))
            await self.safe_send(text_data=json.dumps({"type": "done"}))
            return

        self._greeting_tts_chars += len(greeting)

        async with self._tts_send_lock:
            if self._closed:
                return

            self.session["bot_speaking"] = True
            self._turn_start_time = time.time()
            self._reset_playout_state()
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
                print(f"❌ [TTS] greeting stream broke mid-playback: {e}")
                logger.error(f"[TTS] greeting stream broke (session={self.session_id}): {e}")
                self._log_error("tts", "greeting_stream", e, severity="error", bytes_sent=total_bytes_sent)
            finally:
                self._arm_bot_speaking_fallback(
                    total_bytes_sent, self.BOT_AUDIO_SAMPLE_RATE, tag="greeting"
                )

    def _build_recording_basename(self):
        def _clean(value):
            if not value:
                return ""
            value = str(value).strip().upper()
            value = re.sub(r'[^A-Z0-9]+', '_', value)
            return value.strip('_')

        customer_code = _clean(self.customer_id) or _clean(self.phone_number)
        parts = [p for p in ("RECOVERY", customer_code) if p]
        if not parts:
            parts = ["CALL", self.session_id[:8]]
        return "_".join(parts)

    async def save_conversation_recording(self):
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

        stereo = np.empty(n * 2, dtype=np.int16)
        stereo[0::2] = left
        stereo[1::2] = right

        stereo_wav = os.path.join(temp_dir, f"{filename}_stereo_temp.wav")
        with wave.open(stereo_wav, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.RECORD_SAMPLE_RATE)
            wf.writeframes(stereo.tobytes())

        from pydub import AudioSegment
        stereo_seg = AudioSegment.from_wav(stereo_wav)
        stereo_mp3 = os.path.join(temp_dir, f"{filename}_stereo.mp3")
        stereo_seg.export(stereo_mp3, format="mp3", bitrate="96k")
        print(f"📱 [RECORD] Stereo saved (L=user, R=bot): {stereo_mp3}")

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
                self.session_id, stereo_mp3, mixed_mp3,
            )
            if updated:
                print(f"💾 [RECORD] CallSession updated with recording paths")
            else:
                print(f"⚠️ [RECORD] No CallSession row found for session_id={self.session_id}")
        except Exception as e:
            print(f"❌ [RECORD] Failed to persist recording paths to DB: {e}")
            self._log_error("db", "_persist_recording_paths_sync", e, severity="error",
                            duration_seconds=round(duration_seconds, 1))

        metadata = {
            "session_id": self.session_id,
            "start_time": self.recording["start_time"],
            "end_time": time.time(),
            "duration_seconds": duration_seconds,
            "transcript": self.recording["transcript"],
            "files": {"stereo": stereo_mp3, "mixed": mixed_mp3},
        }

        meta_path = os.path.join(temp_dir, f"{filename}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        for p in (stereo_wav, mono_wav):
            if os.path.exists(p):
                os.remove(p)

        return stereo_mp3
    