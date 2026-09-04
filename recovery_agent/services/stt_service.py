"""
stt_service.py

Unified STT layer: THREE backends wired up --
    - Gnani WebSocket streaming   (current PRIMARY / target -- see STT_BACKEND)
    - Sarvam WebSocket streaming  (kept fully working, previous primary)
    - Google Cloud Streaming STT  (automatic per-utterance fallback for
      whichever of the above is primary, unchanged from before)

Public interface (used by consumers.py / dialer.py) -- UNCHANGED:

    stt = STTSession(sample_rate=8000, language_code="hi-IN", phrases=STT_PHRASES)
    await stt.connect()                    # ONCE, at call connect()

    # ---- per utterance, driven by local VAD exactly as before ----
    stt.feed(chunk)                        # EVERY audio chunk, speech ho ya na ho
    stt.begin_turn()                       # at local VAD speech-start
    transcript = await stt.end_turn(rescue_audio=utterance_bytes)   # at speech-end

    await stt.close()                      # ONCE, at call disconnect()

🔁 BACKEND SWITCH: everything above stays IDENTICAL no matter which
streaming provider is actually being used underneath. That choice is a
single module-level constant:

    STT_BACKEND = "gnani"   # or "sarvam"

Flip that one line (or pass backend="sarvam"/"gnani" into STTSession(...))
to switch providers. consumers.py / dialer.py need ZERO changes either way.

--------------------------------------------------------------------------
CHANGELOG (Sarvam rewrite -- kept for reference, code untouched below):
  1. Keepalive ab {"type":"ping"} NAHI bhejta. Sarvam ke STT socket par ping
     message exist hi nahi karta (SDK sarvamai/speech_to_text_streaming/
     socket_client.py me sirf transcribe() aur flush() hain -- ping sirf TTS
     socket par hai). Server usse audio message samajh kar
     "'audio' must not be None" error deta tha aur connection tod deta tha.
     Ab idle keepalive = 100ms silent PCM frame (valid audio message).
  2. Query param names SDK ke raw_client.py se match kiye gaye:
     sample_rate / input_audio_codec (underscore), language-code (hyphen).
     Pehle "sample-rate" bheja ja raha tha jo server silently ignore karta tha
     -- 8kHz telephony (dialer.py) par ye alone hi Sarvam ko tod deta tha.
  3. Pre-roll ring buffer (500ms): feed() ab turn ke bahar bhi call ho sakta hai,
     audio ring me jaata hai aur begin_turn() par Sarvam ko bheja jaata hai,
     taaki word ka onset na kate.
  4. Fine VAD params explicitly set (first_turn_min_speech_frames etc.) taaki
     Sarvam apni taraf se onset dobara trim na kare.
  5. Turn sequence guard: late-arriving final agle turn ka transcript corrupt
     nahi karta.
  6. Normal close (1000) par bhi reconnect hota hai; send loop har exception
     par connection ko dead mark karta hai (pehle zombie ban jaata tha).
  7. end_turn(rescue_audio=...) -- Sarvam khaali laut aaye to wahi utterance
     Google par ek baar retry hoti hai, turn poora waste nahi hota.

CHANGELOG (Gnani addition -- this pass):
  1. New PersistentGnaniSTT class, same call-scoped lifecycle shape as
     PersistentSarvamSTT (connect/begin_turn/feed/discard_preroll/end_turn/
     close), but adapted to Gnani's actual protocol:
       - Auth/config via WebSocket UPGRADE HEADERS (x-api-key-id, lang_code,
         x-sample-rate, x-format), not query params, and not changeable
         mid-session.
       - Raw binary PCM frames, NOT JSON-wrapped base64 like Sarvam -- and
         they must be EXACTLY 1024 bytes, sent at a strict real-time
         cadence (32ms @16kHz / 64ms @8kHz). Sending late/early or wrong
         size degrades Gnani's own VAD.
       - No client-side "flush" message exists in Gnani's protocol -- the
         server runs its own VAD continuously and emits a `transcript`
         message whenever IT detects a completed segment. So instead of a
         flush/ack round-trip, this class runs ONE continuous frame-pacing
         sender for the whole call (started in connect(), stopped in
         close()), silence-padding whenever feed() hasn't supplied enough
         real audio to fill the next frame. begin_turn()/end_turn() don't
         touch the socket at all -- they just mark which incoming
         `transcript` belongs to the local turn and wait (bounded timeout)
         for it.
  2. STTSession now wires up BOTH PersistentGnaniSTT and PersistentSarvamSTT
     unconditionally; STT_BACKEND decides which one connect()/begin_turn()/
     feed()/end_turn()/discard_preroll() actually route to. Google fallback
     behaviour (auto per-utterance fallback if primary is down, and
     rescue-on-empty-transcript) is preserved for BOTH primaries.

Timing attributes (epoch seconds, None until reached), read AFTER end_turn():
    stt.start_time         -- this turn's begin_turn() time
    stt.first_result_time  -- first transcript-related signal seen for this turn
    stt.finish_time         -- end_turn() returned
    stt.backend             -- "gnani", "sarvam", "google", or "google-rescue",
                                whichever actually served THIS turn
"""

import asyncio
import base64
import io
import json
import logging
import queue
import ssl
import threading
import time
import wave
from urllib.parse import urlencode

import certifi
import websockets
from django.conf import settings

logger = logging.getLogger('voice_bot')

SARVAM_WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"

SARVAM_CONNECT_TIMEOUT = 3.0

# Ab jab sample_rate sach me apply ho raha hai aur onset nahi kat raha, flush
# ka ack reliably aata hai -- isliye thoda sa zyada wait affordable hai. Phir
# bhi safety net hi hai: timeout par last interim transcript use hota hai.
SARVAM_FLUSH_TIMEOUT = 0.6

# Confirmed from sarvamai SDK: SttFlushSignal -> {"type": "flush"}.
# Requires flush_signal=true as a CONNECTION param (see connect()).
SARVAM_FLUSH_MESSAGE = json.dumps({"type": "flush"})

_FLUSH_SENTINEL = object()

# Idle keepalive: 100ms of silence, sent as a normal audio message.
KEEPALIVE_INTERVAL = 5.0

# ============================================================
# GNANI -- current target PRIMARY backend (see STT_BACKEND below)
# ============================================================
GNANI_WS_URL = "wss://api.vachana.ai/stt/v3/stream"
GNANI_CONNECT_TIMEOUT = 3.0

# How long end_turn() waits for Gnani's own server-side VAD to notice the
# local speech-end and emit a `transcript` message for this segment. Gnani
# has no client-driven flush, so this is a pure wait, not a round-trip ack
# -- give it a bit more room than Sarvam's flush timeout.
GNANI_RESULT_TIMEOUT = 1.5

GNANI_FRAME_BYTES = 1024      # exact frame size Gnani's protocol requires
GNANI_FRAME_SAMPLES = 512     # 1024 bytes / 2 bytes-per-sample (16-bit PCM)

# 🔁 SINGLE SWITCH POINT: which persistent streaming backend STTSession
# treats as primary. "gnani" = current target. Set this to "sarvam" to roll
# back instantly -- nothing else in this file, or in consumers.py/dialer.py,
# needs to change; both backends stay fully wired either way.
STT_BACKEND = "gnani"   # "gnani" | "sarvam"

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

_google_stt_client = None


def _get_google_client():
    global _google_stt_client
    if _google_stt_client is None:
        from google.cloud import speech
        _google_stt_client = speech.SpeechClient()
    return _google_stt_client


# ============================================================
# SARVAM STREAMING -- ONE CONNECTION PER CALL (kept working, untouched)
# ============================================================

class PersistentSarvamSTT:
    """ONE instance per PHONE CALL. Pure asyncio -- ek hi event loop se use karo."""

    # Pre-roll ring: itne ms ka audio hamesha yaad rakho, taaki VAD ke
    # speech-start se PEHLE ka onset bhi Sarvam ko mile.
    PREROLL_MS = 500

    def __init__(self, sample_rate=8000, language_code="hi-IN", mode="codemix",
                 vad_signals=True, high_vad_sensitivity=True):
        self.sample_rate = sample_rate
        self.language_code = language_code
        self.mode = mode
        self.vad_signals = vad_signals
        self.high_vad_sensitivity = high_vad_sensitivity

        self.connected = False
        self._ws = None
        self._send_queue: "asyncio.Queue" = asyncio.Queue(maxsize=50)
        self._send_task = None
        self._recv_task = None
        self._keepalive_task = None
        self._closed = False
        self._connect_error = None
        self._consecutive_timeouts = 0
        self._reconnecting = False

        # Per-turn state
        self.start_time = None
        self.first_result_time = None
        self.finish_time = None
        self._turn_transcript = ""
        self._turn_final_event = asyncio.Event()
        self._awaiting_flush = False
        self._turn_active = False
        self._turn_seq = 0          # har begin_turn() par badhta hai
        self._flush_seq = None      # kis turn ka flush pending hai

        # Pre-roll ring buffer (bytes). 16-bit mono => 2 bytes/sample.
        self._preroll_max = int(self.sample_rate * 2 * self.PREROLL_MS / 1000)
        self._preroll = bytearray()

        self._last_send_time = 0.0

        self.last_vad_signal = None
        self.last_vad_signal_time = None

    # -------------------- call lifecycle --------------------

    async def connect(self, timeout=SARVAM_CONNECT_TIMEOUT) -> bool:
        """Call ONCE at call connect() (fire-and-forget task ideally, taaki
        greeting playback ke saath overlap ho jaaye)."""
        params = {
            # NOTE: sirf language-code hyphenated hai. Baaki sab underscore.
            # Verified against sarvamai SDK raw_client.py encode_query().
            "language-code": self.language_code,
            "model": "saaras:v3",
            "mode": self.mode,
            "sample_rate": str(self.sample_rate),
            "input_audio_codec": "wav",
            "flush_signal": "true",
            "vad_signals": "true" if self.vad_signals else "false",
            "high_vad_sensitivity": "true" if self.high_vad_sensitivity else "false",
            # Fine VAD tuning -- server-side trimming ko dheela karo. Local VAD
            # already turn boundaries decide karta hai, isliye Sarvam ko dobara
            # aggressive trim karne ki zaroorat nahi. Default first-turn value
            # (8 frames ~ 256ms) chhote "hello"/"haan" type utterances ko poora
            # nigal jaata tha.
            "min_speech_frames": "2",
            "first_turn_min_speech_frames": "2",
            "pre_speech_pad_frames": "15",
        }
        ws_url = f"{SARVAM_WS_URL}?{urlencode(params)}"
        headers = {"api-subscription-key": settings.SARVAM_API_KEY}

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    ws_url, additional_headers=headers,
                    ping_interval=20, ping_timeout=20, max_size=None,
                    ssl=_SSL_CONTEXT,
                ),
                timeout=timeout,
            )
        except Exception as e:
            logger.error(f"[SarvamSTT] persistent connect failed: {type(e).__name__}: {e}")
            print(f"❌ [SarvamSTT] persistent connect failed: {type(e).__name__}: {e}")
            self._connect_error = e
            self.connected = False
            return False

        self.connected = True
        self._consecutive_timeouts = 0
        self._last_send_time = time.time()
        # Purani queue me agar dead-connection ka koi bacha hua item hai to
        # naye socket par mat bhejo.
        self._send_queue = asyncio.Queue(maxsize=50)
        self._send_task = asyncio.create_task(self._send_loop())
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        print(f"✅ [SarvamSTT] persistent connection established "
              f"(sample_rate={self.sample_rate}, mode={self.mode}, lang={self.language_code})")
        return True

    async def _reconnect(self):
        """Best-effort reconnect. connected=False rehta hai jab tak safal na ho,
        taaki har turn transparently Google par chala jaaye."""
        if self._reconnecting or self._closed:
            return
        self._reconnecting = True
        try:
            self.connected = False
            current = asyncio.current_task()
            for task in (self._send_task, self._recv_task, self._keepalive_task):
                if task and task is not current and not task.done():
                    task.cancel()
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            ok = await self.connect()
            if ok:
                print("✅ [SarvamSTT] reconnected successfully after dead-connection detection")
            else:
                print("❌ [SarvamSTT] reconnect attempt failed -- staying on Google fallback for now")
        finally:
            self._reconnecting = False

    def _mark_dead(self, reason: str):
        """Connection ko dead mark karo aur background me reconnect try karo."""
        if self._closed:
            return
        was_connected = self.connected
        self.connected = False
        if self._awaiting_flush:
            self._awaiting_flush = False
            self._turn_final_event.set()
        if was_connected:
            logger.warning(f"[SarvamSTT] connection marked dead: {reason}")
            print(f"⚠️ [SarvamSTT] connection dead ({reason}) -- reconnecting in background")
            asyncio.create_task(self._reconnect())

    async def _keepalive_loop(self):
        """Idle keepalive.

        ⚠️ Yahan PEHLE {"type":"ping"} bheja ja raha tha -- Sarvam ke STT socket
        par wo message type hai hi nahi (ping sirf TTS socket par hai). Server
        use audio message samajh kar "'audio' must not be None" error deta tha
        aur pipeline gira deta tha. Uske badle ab 100ms silent PCM bhejte hain:
        ye ek bilkul valid audio message hai, socket idle-dead nahi hota, aur
        VAD par koi asar nahi padta (silence hai)."""
        silence_bytes = b"\x00" * int(self.sample_rate * 2 * 0.1)  # 100ms
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._closed or self._ws is None or not self.connected:
                    return
                if self._turn_active:
                    continue  # real audio already ja raha hai
                if time.time() - self._last_send_time < KEEPALIVE_INTERVAL:
                    continue
                try:
                    self._send_queue.put_nowait(silence_bytes)
                except asyncio.QueueFull:
                    pass
        except asyncio.CancelledError:
            pass

    async def close(self):
        """Call ONCE, at call disconnect()."""
        self._closed = True
        self.connected = False
        for task in (self._send_task, self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # -------------------- per-turn lifecycle --------------------

    def begin_turn(self):
        """Call at local-VAD speech-start. Pre-roll ring ko turant socket par
        flush karta hai taaki utterance ka onset na kate."""
        self.start_time = time.time()
        self.first_result_time = None
        self.finish_time = None
        self._turn_transcript = ""
        self._turn_final_event = asyncio.Event()
        self._awaiting_flush = False
        self._turn_seq += 1
        self._turn_active = True

        if self._preroll:
            preroll = bytes(self._preroll)
            self._preroll = bytearray()
            self._enqueue(preroll)

    def feed(self, chunk: bytes):
        """HAR audio chunk par call karo -- speech ke andar bhi, bahar bhi.

        Turn ke bahar audio pre-roll ring me jaata hai (socket par nahi), turn
        ke andar seedha socket par. Isliye consumers.py/dialer.py ko VAD gate
        se pehle hi feed() call karna chahiye."""
        if not chunk:
            return
        if not self._turn_active:
            self._preroll.extend(chunk)
            if len(self._preroll) > self._preroll_max:
                del self._preroll[:len(self._preroll) - self._preroll_max]
            return
        self._enqueue(chunk)

    def discard_preroll(self):
        """Drop any buffered pre-roll audio WITHOUT sending it to Sarvam.

        Used on confirmed barge-in: the sustain-window audio the caller
        already captured (buffered_audio in consumers.py) contains the
        true onset of what the user just said. The pre-roll ring at that
        moment instead holds whatever was recorded just before that --
        typically the bot's own speech leaking into the mic -- and must
        not be prepended to the fresh turn. Safe to call whether or not a
        turn is currently active; it only ever touches the idle ring,
        never the socket."""
        self._preroll = bytearray()

    def _enqueue(self, chunk: bytes):
        if not (self.connected and not self._closed):
            return
        try:
            self._send_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._send_queue.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass
            logger.warning("[SarvamSTT] send queue full -- dropped oldest chunk")
            try:
                self._send_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    async def end_turn(self, timeout=SARVAM_FLUSH_TIMEOUT) -> str:
        """Call at local-VAD speech-end. Already-open socket par flush bhejta
        hai aur is turn ka final transcript await karta hai."""
        turn_seq = self._turn_seq
        self._turn_active = False

        if not self.connected or self._ws is None:
            self.finish_time = time.time()
            print("⚠️ [SarvamSTT] end_turn() called on a dead connection -- returning empty")
            return ""

        self._awaiting_flush = True
        self._flush_seq = turn_seq
        flush_sent_at = time.time()
        try:
            await self._send_queue.put(_FLUSH_SENTINEL)
            await asyncio.wait_for(self._turn_final_event.wait(), timeout=timeout)
            print(f"✅ [SarvamSTT] flush ack'd in {(time.time() - flush_sent_at) * 1000:.0f}ms")
            self._consecutive_timeouts = 0
        except asyncio.TimeoutError:
            self._awaiting_flush = False
            logger.warning(
                f"[SarvamSTT] end_turn() hit the {timeout}s safety-net timeout -- "
                f"falling back to last interim transcript "
                f"(partial so far={self._turn_transcript!r})"
            )
            print(f"⏱️ [SarvamSTT] flush TIMEOUT after {timeout}s -- "
                  f"using last interim: {self._turn_transcript!r}")
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= 3 and not self._turn_transcript:
                logger.error(
                    f"[SarvamSTT] {self._consecutive_timeouts} consecutive EMPTY timeouts -- "
                    f"treating connection as dead"
                )
                self._mark_dead(f"{self._consecutive_timeouts} consecutive empty timeouts")
        finally:
            self._flush_seq = None
        self.finish_time = time.time()
        return self._turn_transcript

    # -------------------- internals --------------------

    async def _send_loop(self):
        """Runs for the life of the call. Har message ek self-contained WAV blob
        hai, base64 me, {"audio": {...}} envelope ke andar -- yahi format SDK ka
        transcribe() bhejta hai."""
        try:
            while True:
                item = await self._send_queue.get()
                if item is _FLUSH_SENTINEL:
                    print("📤 [SarvamSTT] sending flush signal")
                    await self._ws.send(SARVAM_FLUSH_MESSAGE)
                    self._last_send_time = time.time()
                    continue
                wav_bytes = self._pcm_to_wav(item, self.sample_rate)
                audio_b64 = base64.b64encode(wav_bytes).decode("utf-8")
                message = json.dumps({
                    "audio": {
                        "data": audio_b64,
                        # int, string nahi -- SDK ka AudioData.sample_rate int hai.
                        "sample_rate": self.sample_rate,
                        "encoding": "audio/wav",
                    }
                })
                await self._ws.send(message)
                self._last_send_time = time.time()
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed:
            self._mark_dead("send loop: connection closed")
        except Exception as e:
            # Pehle yahan sirf ConnectionClosed catch hota tha -- koi bhi doosri
            # exception task ko chupchaap maar deti thi aur connected=True hi
            # reh jaata tha, yaani har agla turn ek zombie socket par jaata tha.
            logger.error(f"[SarvamSTT] send loop crashed: {type(e).__name__}: {e}")
            self._mark_dead(f"send loop crash: {type(e).__name__}")

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")
                print(f"📥 [SarvamSTT] recv type={event_type!r} awaiting_flush={self._awaiting_flush}")

                if event_type == "data":
                    result = data.get("data", {})
                    transcript = result.get("transcript")
                    # Turn guard: flush timeout ke baad aane wala late final
                    # ab AGLE turn ka transcript corrupt nahi karega.
                    accept = self._turn_active or (
                        self._awaiting_flush and self._flush_seq == self._turn_seq
                    )
                    if accept:
                        if self.first_result_time is None:
                            self.first_result_time = time.time()
                        if transcript:
                            self._turn_transcript = transcript
                    elif transcript:
                        logger.info(f"[SarvamSTT] ignoring out-of-turn transcript: {transcript!r}")
                    if self._awaiting_flush:
                        self._awaiting_flush = False
                        self._turn_final_event.set()

                elif event_type == "error":
                    logger.error(f"[SarvamSTT] server error: {data}")
                    print(f"❌ [SarvamSTT] server error: {data}")
                    if self._awaiting_flush:
                        self._awaiting_flush = False
                        self._turn_final_event.set()

                elif event_type in ("speech_start", "speech_end"):
                    self.last_vad_signal = event_type
                    self.last_vad_signal_time = time.time()

                elif event_type == "events":
                    signal_type = (data.get("data") or {}).get("signal_type")
                    self.last_vad_signal = signal_type
                    self.last_vad_signal_time = time.time()

            # ⚠️ Yahan pahunchne ka matlab: socket NORMALLY close hua (code 1000).
            # websockets ka `async for` normal close par exception nahi phenkta,
            # isliye purana code chupchaap yahin exit ho jaata tha -- connected
            # True hi reh jaata, koi reconnect nahi hota, aur har agla turn ek
            # dead socket par jaata tha.
            self._mark_dead("receive loop: socket closed normally by server")

        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed as e:
            self._mark_dead(f"receive loop: connection closed ({e})")
        except Exception as e:
            logger.error(f"[SarvamSTT] receive loop crashed: {type(e).__name__}: {e}")
            self._mark_dead(f"receive loop crash: {type(e).__name__}")

    @staticmethod
    def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        buf.seek(0)
        return buf.read()


# ============================================================
# GNANI STREAMING -- ONE CONNECTION PER CALL (current target primary)
# ============================================================

class PersistentGnaniSTT:
    """ONE instance per PHONE CALL. Mirrors PersistentSarvamSTT's public
    shape (connect/begin_turn/feed/discard_preroll/end_turn/close), but the
    internals are adapted to how Gnani's /stt/v3/stream protocol actually
    works, which is meaningfully different from Sarvam's:

      - Config (api key, language, sample rate, ITN format) goes in the
        WebSocket UPGRADE HEADERS, not query params, and can't change
        mid-session (reconnect required to change settings).
      - Audio is sent as RAW BINARY PCM frames -- no JSON envelope, no
        base64 -- and every frame must be EXACTLY 1024 bytes, sent at a
        strict real-time cadence (32ms @16kHz / 64ms @8kHz). The docs are
        explicit that bursting/buffering degrades VAD accuracy.
      - There is NO client-side flush message. The server runs its own VAD
        continuously over the live stream and emits a `transcript` message
        per detected segment on its own -- this endpoint is literally
        designed for "live microphone / phone call / real-time audio".

    To fit that into our local-VAD-driven begin_turn()/end_turn() model,
    this class runs ONE continuous frame-pacing sender for the whole call
    (started in connect(), stopped in close()). feed() just appends bytes
    to a buffer; the pacing loop drains it at the exact cadence Gnani
    requires, silence-padding whenever there isn't enough real audio yet
    so the stream never stalls. begin_turn()/end_turn() don't touch the
    socket at all -- they mark which incoming `transcript` belongs to the
    current local turn and wait (bounded timeout) for it to arrive.

    Note: consumers.py only calls feed() while NOT bot_speaking and NOT
    is_processing (see handle_audio in consumers.py) -- so, exactly like
    Sarvam's pre-roll ring, real customer audio during those windows never
    reaches Gnani at all; the pacing loop just sends silence padding
    during those gaps, same effect as Sarvam's separate ring buffer.
    """

    def __init__(self, sample_rate=16000, language_code="hi-IN",
                 x_format="verbatim", itn_native_numerals=False):
        self.sample_rate = sample_rate
        self.language_code = language_code
        # "verbatim" = raw spoken-form text (matches Sarvam's behaviour most
        # closely, and keeps existing transcript-parsing code -- e.g.
        # extract_slot_request()/mentions_confirmation() -- working
        # unchanged). Set to "transcribe" to enable Gnani's ITN (digits/
        # currency/dates/times normalized) if/when that parsing is updated
        # to expect it -- see the STT docs' ITN section.
        self.x_format = x_format
        self.itn_native_numerals = itn_native_numerals

        self.connected = False
        self._ws = None

        # Single buffer feed() appends into; the pacing loop drains it.
        # No separate "turn" vs "pre-roll" ring needed (unlike Sarvam) --
        # Gnani streams continuously either way.
        self._pending_buffer = bytearray()
        self._pending_buffer_max = sample_rate * 2 * 5  # ~5s safety cap only

        self._pace_task = None
        self._recv_task = None
        self._closed = False
        self._connect_error = None
        self._consecutive_timeouts = 0
        self._reconnecting = False

        # Per-turn state (same shape as PersistentSarvamSTT for parity)
        self.start_time = None
        self.first_result_time = None
        self.finish_time = None
        self._turn_transcript = ""
        self._turn_final_event = asyncio.Event()
        self._awaiting_flush = False
        self._turn_active = False
        self._turn_seq = 0
        self._flush_seq = None

        # 🔥 Gnani ki server-side VAD ne end-of-speech detect kiya -- caller
        # (dialer/consumers) isi par turn dispatch kar sakta hai, apna local
        # silence-counter poora kiye bina. None = koi listener nahi.
        self.on_speech_end = None

    # -------------------- call lifecycle --------------------

    async def connect(self, timeout=GNANI_CONNECT_TIMEOUT) -> bool:
        """Call ONCE at call connect(). Starts the receive loop AND the
        continuous frame-pacing sender -- both live for the whole call."""
        headers = {
            "x-api-key-id": settings.GNANI_API_KEY,
            "lang_code": self.language_code,
            "x-sample-rate": str(self.sample_rate),
            "x-format": self.x_format,
            "itn_native_numerals": "true" if self.itn_native_numerals else "false",
        }
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    GNANI_WS_URL, additional_headers=headers,
                    ping_interval=20, ping_timeout=20, max_size=None,
                    ssl=_SSL_CONTEXT,
                ),
                timeout=timeout,
            )
        except Exception as e:
            logger.error(f"[GnaniSTT] persistent connect failed: {type(e).__name__}: {e}")
            print(f"❌ [GnaniSTT] persistent connect failed: {type(e).__name__}: {e}")
            self._connect_error = e
            self.connected = False
            return False
        if self._closed:
            try:
                await self._ws.close()
            except Exception:
                pass
            self.connected = False
            print("⚠️ [GnaniSTT] connect() finished after close() already ran -- closing orphaned socket")
            return False  
        
        self.connected = True
        self._consecutive_timeouts = 0
        self._pending_buffer = bytearray()
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._pace_task = asyncio.create_task(self._pacing_loop())
        print(f"✅ [GnaniSTT] persistent connection established "
              f"(sample_rate={self.sample_rate}, format={self.x_format}, lang={self.language_code})")
        return True

    async def _reconnect(self):
        """Best-effort reconnect. connected=False rehta hai jab tak safal na ho,
        taaki har turn transparently Google par chala jaaye."""
        if self._reconnecting or self._closed:
            return
        self._reconnecting = True
        try:
            self.connected = False
            current = asyncio.current_task()
            for task in (self._pace_task, self._recv_task):
                if task and task is not current and not task.done():
                    task.cancel()
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            ok = await self.connect()
            if ok:
                print("✅ [GnaniSTT] reconnected successfully after dead-connection detection")
            else:
                print("❌ [GnaniSTT] reconnect attempt failed -- staying on Google fallback for now")
        finally:
            self._reconnecting = False

    def _mark_dead(self, reason: str):
        if self._closed:
            return
        was_connected = self.connected
        self.connected = False
        if self._awaiting_flush:
            self._awaiting_flush = False
            self._turn_final_event.set()
        if was_connected:
            logger.warning(f"[GnaniSTT] connection marked dead: {reason}")
            print(f"⚠️ [GnaniSTT] connection dead ({reason}) -- reconnecting in background")
            asyncio.create_task(self._reconnect())

    async def close(self):
        """Call ONCE, at call disconnect()."""
        self._closed = True
        self.connected = False
        for task in (self._pace_task, self._recv_task):
            if task and not task.done():
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # -------------------- per-turn lifecycle --------------------

    def begin_turn(self):
        """Call at local-VAD speech-start. Doesn't touch the socket -- the
        pacing loop is already streaming continuously -- just marks the
        boundary used to attribute the next `transcript` event to this
        turn."""
        self.start_time = time.time()
        self.first_result_time = None
        self.finish_time = None
        self._turn_transcript = ""
        self._turn_final_event = asyncio.Event()
        self._awaiting_flush = False
        self._turn_seq += 1
        self._turn_active = True

    def feed(self, chunk: bytes):
        """HAR audio chunk par call karo. No turn-gating needed -- Gnani
        streams continuously; the pacing loop pulls from this buffer
        whenever real audio is available, silence otherwise."""
        if not chunk:
            return
        self._pending_buffer.extend(chunk)
        if len(self._pending_buffer) > self._pending_buffer_max:
            # Safety net only -- shouldn't happen if feed() keeps pace with
            # real-time audio, but avoid unbounded growth if it ever does.
            overflow = len(self._pending_buffer) - self._pending_buffer_max
            del self._pending_buffer[:overflow]

    def discard_preroll(self):
        """Drop whatever's sitting in the pending buffer, unsent, WITHOUT
        forwarding it. Same purpose as PersistentSarvamSTT.discard_preroll():
        on a confirmed barge-in, avoid leaking the bot's own speech (picked
        up by the mic just before the barge-in was confirmed) into the
        fresh turn."""
        self._pending_buffer = bytearray()

    # stt_service.py -- STTSession.end_turn()
    async def end_turn(self, timeout=GNANI_RESULT_TIMEOUT) -> str:
        """Call at local-VAD speech-end. The socket keeps streaming
        (silence-padded if needed) in the background -- this just waits for
        Gnani's own VAD to notice the pause and emit this segment's
        transcript."""
        turn_seq = self._turn_seq
        self._turn_active = False

        if not self.connected or self._ws is None:
            self.finish_time = time.time()
            print("⚠️ [GnaniSTT] end_turn() called on a dead connection -- returning empty")
            return ""

        self._awaiting_flush = True
        self._flush_seq = turn_seq
        wait_started_at = time.time()
        try:
            await asyncio.wait_for(self._turn_final_event.wait(), timeout=timeout)
            print(f"✅ [GnaniSTT] transcript received in {(time.time() - wait_started_at) * 1000:.0f}ms")
            self._consecutive_timeouts = 0
        except asyncio.TimeoutError:
            self._awaiting_flush = False
            logger.warning(
                f"[GnaniSTT] end_turn() hit the {timeout}s safety-net timeout -- "
                f"no transcript arrived (partial so far={self._turn_transcript!r})"
            )
            print(f"⏱️ [GnaniSTT] wait TIMEOUT after {timeout}s -- "
                f"transcript so far: {self._turn_transcript!r}")
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= 3 and not self._turn_transcript:
                logger.error(
                    f"[GnaniSTT] {self._consecutive_timeouts} consecutive EMPTY timeouts -- "
                    f"treating connection as dead"
                )
                self._mark_dead(f"{self._consecutive_timeouts} consecutive empty timeouts")
        finally:
            self._flush_seq = None
        self.finish_time = time.time()
        return self._turn_transcript

    # -------------------- internals --------------------

    async def _pacing_loop(self):
        """Continuous real-time frame sender -- lives for the whole call,
        matching Gnani's "live microphone / phone call" streaming model
        exactly. Sends exactly GNANI_FRAME_BYTES every frame_duration
        seconds; silence-pads whenever feed() hasn't kept up, so cadence
        never breaks (breaking cadence degrades the server's VAD per the
        docs)."""
        frame_duration = GNANI_FRAME_SAMPLES / float(self.sample_rate)
        next_time = time.monotonic()
        try:
            while not self._closed:
                if self.connected and self._ws is not None:
                    frame = self._pop_frame()
                    try:
                        await self._ws.send(frame)
                    except websockets.ConnectionClosed:
                        self._mark_dead("pacing loop: connection closed")
                    except Exception as e:
                        logger.error(f"[GnaniSTT] pacing loop send failed: {type(e).__name__}: {e}")
                        self._mark_dead(f"pacing loop send crash: {type(e).__name__}")

                next_time += frame_duration
                sleep_for = next_time - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    # Fell behind (e.g. a GC pause) -- resync instead of
                    # trying to "catch up" with a burst of zero-delay sends,
                    # which is exactly what the docs warn against.
                    next_time = time.monotonic()
        except asyncio.CancelledError:
            pass

    def _pop_frame(self) -> bytes:
        if len(self._pending_buffer) >= GNANI_FRAME_BYTES:
            frame = bytes(self._pending_buffer[:GNANI_FRAME_BYTES])
            del self._pending_buffer[:GNANI_FRAME_BYTES]
            return frame
        # Not enough real audio buffered yet -- pad with silence so the
        # frame is still exactly the required size and cadence holds.
        frame = bytes(self._pending_buffer) + b'\x00' * (GNANI_FRAME_BYTES - len(self._pending_buffer))
        self._pending_buffer = bytearray()
        return frame

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")
                print(f"📥 [GnaniSTT] recv type={event_type!r} awaiting_flush={self._awaiting_flush}")

                if event_type == "connected":
                    continue

                elif event_type == "processing":
                    # Low-latency ack that Gnani's VAD detected end-of-speech
                    # and started transcribing -- use it as the "first
                    # result" timing signal, mirroring Sarvam's interim.
                    if self.first_result_time is None:
                        self.first_result_time = time.time()
                    # 🔥 Yahi Gnani ka speech-end signal hai (standalone test
                    # me "⏳ speech-end detected, transcribing..."). Ye hamare
                    # local RMS VAD se PEHLE aata hai aur acoustic model par
                    # chalta hai, energy par nahi -- isliye caller ko turant
                    # bata do taaki wo apna silence wait chhod kar turn
                    # dispatch kar sake.
                    if self._turn_active and self.on_speech_end:
                        try:
                            self.on_speech_end()
                        except Exception as e:
                            logger.error(f"[GnaniSTT] on_speech_end callback failed: {e}")

                elif event_type == "transcript":
                    text = data.get("text", "")
                    if self.first_result_time is None:
                        self.first_result_time = time.time()
                    # Turn guard: mirrors Sarvam's -- a transcript that
                    # arrives after our wait already timed out for THIS
                    # turn_seq doesn't get attributed to a later turn.
                    accept = self._turn_active or (
                        self._awaiting_flush and self._flush_seq == self._turn_seq
                    )
                    if accept:
                        if text:
                            self._turn_transcript = text
                        # 🔥 FIX (bada wala): event ab HAMESHA set hota hai jab
                        # transcript is turn ka hai. Pehle ye sirf tab set hota
                        # tha jab _awaiting_flush True ho -- lekin Gnani apni
                        # khud ki VAD par transcript hamare end_turn() se PEHLE
                        # bhej deta hai (log me `awaiting_flush=False`). Us case
                        # me event kabhi set nahi hota tha, transcript chupchaap
                        # _turn_transcript me pada rehta tha, aur end_turn()
                        # poore GNANI_RESULT_TIMEOUT (1.5s) tak baitha rehta tha
                        # ek aise event ka intezaar karte hue jo aa hi chuka tha.
                        # Yahi wajah thi ki standalone test me 200ms dikhne wali
                        # latency call me 500-900ms ho jaati thi.
                        self._awaiting_flush = False
                        self._turn_final_event.set()
                    elif text:
                        logger.info(f"[GnaniSTT] ignoring out-of-turn transcript: {text!r}")

                elif event_type == "error":
                    logger.error(f"[GnaniSTT] server error: {data}")
                    print(f"❌ [GnaniSTT] server error: {data}")
                    if self._awaiting_flush:
                        self._awaiting_flush = False
                        self._turn_final_event.set()

            self._mark_dead("receive loop: socket closed normally by server")

        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed as e:
            self._mark_dead(f"receive loop: connection closed ({e})")
        except Exception as e:
            logger.error(f"[GnaniSTT] receive loop crashed: {type(e).__name__}: {e}")
            self._mark_dead(f"receive loop crash: {type(e).__name__}")


# ============================================================
# GOOGLE STREAMING SESSION -- per-utterance fallback, unchanged
# ============================================================

class GoogleSTTSession:
    """One instance per utterance. Not reusable."""

    def __init__(self, sample_rate=8000, language_code="hi-IN",
                 alt_language_codes=("en-IN",), phrases=None, boost=15.0):
        from google.cloud import speech

        self._speech = speech
        self._audio_queue = queue.Queue()
        self._closed = False
        self._final_transcript = ""
        self._error = None
        self._thread = None

        self.start_time = None
        self.first_result_time = None
        self.finish_time = None

        speech_contexts = []
        if phrases:
            speech_contexts = [speech.SpeechContext(phrases=list(phrases), boost=boost)]

        self._config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            language_code=language_code,
            alternative_language_codes=list(alt_language_codes),
            enable_automatic_punctuation=True,
            model="latest_short",
            speech_contexts=speech_contexts,
        )
        self._streaming_config = speech.StreamingRecognitionConfig(
            config=self._config,
            interim_results=True,
        )

    def _request_generator(self):
        while True:
            chunk = self._audio_queue.get()
            if chunk is None:
                return
            yield self._speech.StreamingRecognizeRequest(audio_content=chunk)

    def _run(self):
        try:
            client = _get_google_client()
            responses = client.streaming_recognize(
                config=self._streaming_config,
                requests=self._request_generator(),
            )
            pieces = []
            for response in responses:
                for result in response.results:
                    if self.first_result_time is None and result.alternatives:
                        self.first_result_time = time.time()
                    if result.is_final and result.alternatives:
                        pieces.append(result.alternatives[0].transcript)
            self._final_transcript = " ".join(pieces).strip()
        except Exception as e:
            logger.error(f"[GoogleSTT] session error: {e}")
            self._error = e

    def start(self):
        self.start_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, chunk: bytes):
        if not self._closed:
            self._audio_queue.put(chunk)

    def finish(self, timeout=15.0) -> str:
        self._closed = True
        self._audio_queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)
        self.finish_time = time.time()
        if self._error and not self._final_transcript:
            return ""
        return self._final_transcript


# ============================================================
# ORCHESTRATOR -- single entry point for consumers.py / dialer.py
# ============================================================

class STTSession:
    """
        stt = STTSession(sample_rate=8000, language_code="hi-IN", phrases=STT_PHRASES)
        await stt.connect()                 # once, at call connect()

        stt.feed(chunk)                     # HAR chunk par (VAD gate se pehle)
        stt.begin_turn()                    # at local VAD speech-start
        transcript = await stt.end_turn(rescue_audio=utterance_bytes)

        await stt.close()                   # once, at call disconnect()

    🔁 BACKEND SWITCH: which streaming STT provider is "primary" is
    controlled by the single module-level constant STT_BACKEND at the top
    of this file ("gnani" or "sarvam"), or by passing backend=... here.
    Both PersistentGnaniSTT and PersistentSarvamSTT are ALWAYS constructed
    below -- only the primary one is ever actually connect()ed -- so
    flipping STT_BACKEND is enough to switch providers without touching
    consumers.py, dialer.py, or anything else that calls into this class.
    Google Cloud STT remains the per-utterance fallback either way,
    unchanged.
    """
    def __init__(self, sample_rate=8000, language_code="hi-IN", phrases=None,
                 vad_signals=True, high_vad_sensitivity=True, backend=None):
        self._sample_rate = sample_rate
        self._language_code = language_code
        # Sarvam ke saaras:v3 aur Gnani dono ke streaming endpoints par
        # word-level phrase/boost nahi hai, isliye phrases sirf Google
        # fallback par kaam aate hain.
        self._phrases = phrases

        self._primary_backend = backend or STT_BACKEND  # "gnani" | "sarvam"

        # Both are always constructed -- only the primary is ever connect()ed
        # (see connect()) -- so switching STT_BACKEND stays a one-line change.
        self._sarvam = PersistentSarvamSTT(
            sample_rate=sample_rate, language_code=language_code,
            vad_signals=vad_signals, high_vad_sensitivity=high_vad_sensitivity,
        )
        self._gnani = PersistentGnaniSTT(
            sample_rate=sample_rate, language_code=language_code,
        )

        self.backend = None
        self._google_fallback = None
        self._on_speech_end = None

        self.start_time = None
        self.first_result_time = None
        self.finish_time = None

    @property
    def on_speech_end(self):
        """Gnani ki server-side VAD ke speech-end par fire hone wala callback.

        Sirf Gnani primary hone par kaam karta hai -- Sarvam/Google par
        caller ka apna local VAD hi turn boundary decide karta rahega,
        bilkul pehle jaisa. Isliye ise set karna safe hai chahe backend
        kuch bhi ho."""
        return self._on_speech_end

    @on_speech_end.setter
    def on_speech_end(self, cb):
        self._on_speech_end = cb
        self._gnani.on_speech_end = cb

    @property
    def _primary(self):
        return self._gnani if self._primary_backend == "gnani" else self._sarvam

    async def connect(self) -> bool:
        ok = await self._primary.connect()
        if not ok:
            logger.warning(
                f"[STTSession] {self._primary_backend} persistent connect failed for this call -- "
                "every turn will fall back to per-utterance Google until it recovers"
            )
            print(f"⚠️ [STTSession] {self._primary_backend} unavailable for this call -- using Google fallback")
        return ok

    def begin_turn(self):
        """Call at local-VAD speech-start."""
        primary = self._primary
        if primary.connected:
            self.backend = self._primary_backend
            self._google_fallback = None
            primary.begin_turn()
        else:
            self.backend = "google"
            self._google_fallback = GoogleSTTSession(
                sample_rate=self._sample_rate,
                language_code=self._language_code,
                phrases=self._phrases,
            )
            self._google_fallback.start()
            # Pre-roll Google ko bhi do, warna wahi onset-clipping problem.
            # Sarvam keeps a dedicated ring (_preroll); Gnani buffers the
            # same pre-turn audio straight into _pending_buffer -- drain
            # whichever one is actually the primary.
            if self._primary_backend == "gnani":
                preroll = bytes(self._gnani._pending_buffer)
                self._gnani._pending_buffer = bytearray()
            else:
                preroll = bytes(self._sarvam._preroll)
                self._sarvam._preroll = bytearray()
            if preroll:
                self._google_fallback.feed(preroll)

    def feed(self, chunk: bytes):
        """HAR audio chunk par call karo -- turn ke andar bhi, bahar bhi.
        Turn ke bahar ka audio pre-roll ke roop me buffer hota hai."""
        if self.backend == "sarvam":
            if self._sarvam._turn_active:
                self._sarvam.feed(chunk)
            # else: shouldn't normally happen (begin_turn() always runs
            # first) -- stay silent rather than risk double-buffering.
        elif self.backend == "gnani":
            self._gnani.feed(chunk)
        elif self._google_fallback is not None:
            self._google_fallback.feed(chunk)
        else:
            # No turn active yet -- buffer as pre-roll on the primary backend.
            if self._primary_backend == "gnani":
                self._gnani.feed(chunk)
            else:
                self._sarvam.feed(chunk)

    def discard_preroll(self):
        """Call on confirmed barge-in, before begin_turn(), to drop stale
        pre-roll audio instead of letting it leak into the fresh turn.

        Routes to whichever backend is the configured primary -- that's
        the only one accumulating pre-roll audio at any given time."""
        if self._primary_backend == "gnani":
            self._gnani.discard_preroll()
        else:
            self._sarvam.discard_preroll()

    async def end_turn(self, rescue_audio: bytes = None, timeout: float = None) -> str:
        """Call at local-VAD speech-end.

        rescue_audio: is turn ka poora utterance PCM. Agar primary backend
        khaali laut aaye (ya beech me mar jaaye) to wahi audio ek baar
        Google par retry hoti hai, taaki user ko dobara bolna na pade.

        timeout: optional override for how long to wait for Gnani's
        transcript (defaults to GNANI_RESULT_TIMEOUT if not passed).
        """
        if self.backend == "gnani":
            if timeout is not None:
                transcript = await self._gnani.end_turn(timeout=timeout)
            else:
                transcript = await self._gnani.end_turn()

            self.start_time = self._gnani.start_time
            self.first_result_time = self._gnani.first_result_time
            self.finish_time = self._gnani.finish_time

            if not transcript:
                logger.warning("[STTSession] Gnani returned empty transcript for this turn")
                if rescue_audio:
                    print("🛟 [STTSession] Gnani empty -- retrying this utterance on Google")
                    transcript = await self._rescue_with_google(rescue_audio)
                    if transcript:
                        self.backend = "google-rescue"
                        print(f"🛟 [STTSession] Google rescue succeeded: {transcript!r}")
            return transcript

        if self.backend == "sarvam":
            transcript = await self._sarvam.end_turn()
            self.start_time = self._sarvam.start_time
            self.first_result_time = self._sarvam.first_result_time
            self.finish_time = self._sarvam.finish_time

            if not transcript:
                logger.warning("[STTSession] Sarvam returned empty transcript for this turn")
                if rescue_audio:
                    print("🛟 [STTSession] Sarvam empty -- retrying this utterance on Google")
                    transcript = await self._rescue_with_google(rescue_audio)
                    if transcript:
                        self.backend = "google-rescue"
                        print(f"🛟 [STTSession] Google rescue succeeded: {transcript!r}")
            return transcript

        if self._google_fallback is not None:
            transcript = await asyncio.to_thread(self._google_fallback.finish)
            self.start_time = self._google_fallback.start_time
            self.first_result_time = self._google_fallback.first_result_time
            self.finish_time = self._google_fallback.finish_time
            return transcript

        self.finish_time = time.time()
        return ""

    async def _rescue_with_google(self, audio_bytes: bytes) -> str:
        """Ek-shot batch recognize -- streaming session khadi karne se sasta."""
        def _run():
            from google.cloud import speech
            client = _get_google_client()
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self._sample_rate,
                language_code=self._language_code,
                alternative_language_codes=["en-IN"],
                enable_automatic_punctuation=True,
                model="latest_short",
                speech_contexts=(
                    [speech.SpeechContext(phrases=list(self._phrases), boost=15.0)]
                    if self._phrases else []
                ),
            )
            resp = client.recognize(
                config=config,
                audio=speech.RecognitionAudio(content=audio_bytes),
            )
            return " ".join(
                r.alternatives[0].transcript for r in resp.results if r.alternatives
            ).strip()

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logger.error(f"[STTSession] Google rescue failed: {e}")
            return ""

    async def close(self):
        """Call ONCE, at call disconnect()."""
        await self._gnani.close()
        await self._sarvam.close()