import os
import requests
import base64
import logging
from django.conf import settings
from murf import Murf, MurfRegion
logger = logging.getLogger('voice_bot')


class BaseTTS:
    def synthesize(self, text, voice_name=None):
        raise NotImplementedError


class GoogleTTS(BaseTTS):
    def __init__(self):
        try:
            from google.cloud import texttospeech
            self.client = texttospeech.TextToSpeechClient()
            self.available = True
        except Exception as e:
            logger.error(f"Google TTS init failed: {e}")
            self.available = False
    
    def synthesize(self, text, voice_name="hi-IN-Wavenet-A"):
        if not self.available:
            raise Exception("Google TTS not available")
        
        from google.cloud import texttospeech
        
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code="hi-IN",
            name=voice_name
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )
        
        response = self.client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config
        )
        return response.audio_content


class SarvamTTS(BaseTTS):
    def __init__(self):
        self.api_key = settings.SARVAM_API_KEY
        self.url = "https://api.sarvam.ai/text-to-speech"
        self.available = bool(self.api_key)
    
    def synthesize(self, text, voice_name="priya"):
        if not self.available:
            raise Exception("Sarvam API key not set")
        
        payload = {
            "inputs": [text],
            "target_language_code": "hi-IN",
            "speaker": voice_name,
            "model": "bulbul:v1"
        }
        
        headers = {
            "api-subscription-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        response = requests.post(self.url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        return base64.b64decode(data["audios"][0])


class MurfTTS(BaseTTS):
    def __init__(self):
        self.api_key = settings.MURF_API_KEY
        self.available = bool(self.api_key)
        
        self._client = None
        try:
            self._client = Murf(
                api_key=self.api_key,
                region=MurfRegion.GLOBAL  # 👈 ye add kar
            )
            print(f"✅ Murf initialized: {self.api_key[:10]}...")
        except Exception as e:
            print(f"❌ Murf init failed: {e}")
            self.available = False
    
    def synthesize_stream(self, text, voice_name="hi-IN-sunaina", cancel_event=None):  # 👈 tera voice_id
        """
        🔥 BARGE-IN FIX: `cancel_event` (a threading.Event, set by
        consumers.py's _cancel_current_turn()/_trigger_barge_in() on
        barge-in) lets a caller actively stop this specific Murf request
        mid-flight.

        What "stop" means for this call: `self._client.text_to_speech.stream(...)`
        is Murf's one-shot REST endpoint -- a single POST with the full
        text, whose response is streamed back chunk-by-chunk over one
        HTTP connection. It is NOT the separate bidirectional websocket
        API (murf.stream_input, with SendText(text=..., end=True) /
        ClearContext) that Murf also offers -- that's a persistent
        multi-turn context connection and would be a much bigger change
        than this codebase wants (every _schedule_tts() call here
        deliberately opens a fresh, independent Murf request, no shared
        context to manage/reset between turns). Confirmed by pulling the
        Murf SDK source directly.

        For a plain streamed HTTP response there is no mid-stream
        "type: end" control message to send. The only real stop signal
        Murf will honor is closing the HTTP connection itself -- which
        is what audio_stream.close() below does (it's the raw
        httpx-backed generator from the SDK; closing it raises
        GeneratorExit inside the SDK's `with ... as _response:` block,
        which tears down the streaming response/connection immediately
        instead of letting it run to completion or waiting for Python's
        GC to eventually close it).

        Known limitation: cancellation is checked between chunks, not
        during a blocking read. If Murf goes quiet mid-stream (rare --
        chunks normally arrive every ~tens of ms), the cancel won't be
        noticed until the next chunk arrives or the read errors out.
        Good enough for barge-in UX; a fully async client would be
        needed to close that last gap.
        """
        if not self.available or not self._client:
            raise Exception("Murf not available")

        if cancel_event is not None and cancel_event.is_set():
            return  # 🔥 already cancelled before we even opened the connection

        print(f"🔥 Streaming: voice={voice_name}, text={text[:20]}...")

        audio_stream = self._client.text_to_speech.stream(
            text=text,
            voice_id=voice_name,  # 👈 exact tera wala
            model="falcon-2",
            locale="hi-IN",
            sample_rate=24000,
            format="PCM"
        )

        try:
            first_chunk = next(audio_stream)
        except StopIteration:
            return

        print(f"🔥 First chunk: {len(first_chunk)} bytes")
        print(f"🔥 First 10 bytes: {list(first_chunk[:10])}")

        if cancel_event is not None and cancel_event.is_set():
            print("🚫 [MURF] cancelled right after first chunk -- closing stream")
            audio_stream.close()
            return

        if first_chunk:
            yield first_chunk

        try:
            for chunk in audio_stream:
                if cancel_event is not None and cancel_event.is_set():
                    print("🚫 [MURF] cancel_event set mid-stream -- closing Murf connection now")
                    audio_stream.close()  # 🔥 actively ends the HTTP stream, not just stops reading it
                    return
                if chunk:
                    yield chunk
        finally:
            # 🔥 belt-and-braces: no matter how this generator exits
            # (natural exhaustion, cancel above, or the caller's `for`
            # loop breaking/GeneratorExit from outside), make sure the
            # underlying HTTP stream is closed and no thread/socket is
            # left dangling.
            close = getattr(audio_stream, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
                
                
    
    def _synthesize_sdk(self, text, voice_name):
        """SDK se generate — base64 decode kar ke bytes return"""
        import inspect
        
        kwargs = {
            "text": text,
            "voice_id": voice_name,
            "locale": "hi-IN",
            "format": "MP3"
        }
        
        # Base64 flag check
        sig = inspect.signature(self._sdk_client.text_to_speech.generate).parameters
        for candidate in ("encode_as_base_64", "encode_as_base64", "encode_output_as_base64", "base64"):
            if candidate in sig:
                kwargs[candidate] = True
                break
        
        result = self._sdk_client.text_to_speech.generate(**kwargs)
        
        # Base64 field dhundho
        for field in ("encoded_audio", "audio_base64", "audio_content"):
            encoded = getattr(result, field, None)
            if encoded:
                return base64.b64decode(encoded)
        
        # URL se download karo
        for field in ("audio_file", "audio_file_url", "url", "audio_url"):
            url = getattr(result, field, None)
            if url:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
        
        raise RuntimeError(f"Murf response had no audio: {result}")
    
    def _synthesize_rest(self, text, voice_name):
        """REST API se generate"""
        url = f"{self.BASE_URL}/speech/generate"
        
        payload = {
            "text": text,
            "voiceId": voice_name,
            "model": "falcon-2",
            "style": "Conversational",
            "locale": "hi-IN",
            "sampleRate": 24000,
            "format": "MP3",
            "channel": "MONO",
            "encodeAsBase64": True
        }
        
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        return base64.b64decode(data["audioBase64"])
    
    
    def _synthesize_stream_sdk(self, text, voice_name):
        """SDK se stream — direct PCM chunks"""
        audio_stream = self._sdk_client.text_to_speech.stream(
            text=text,
            voice_id=voice_name,
            model="falcon-2",
            locale="hi-IN",
            sample_rate=self.STREAM_SAMPLE_RATE,
            format="PCM",
        )
        
        for chunk in audio_stream:
            if chunk:
                yield chunk
    
    def _synthesize_stream_rest(self, text, voice_name):
        """REST API se stream"""
        url = f"{self.BASE_URL}/speech/stream"
        
        payload = {
            "text": text,
            "voiceId": voice_name,
            "model": "falcon-2",
            "locale": "hi-IN",
            "sampleRate": self.STREAM_SAMPLE_RATE,
            "format": "PCM",
            "stream": True
        }
        
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, json=payload, headers=headers, 
                                stream=True, timeout=30)
        response.raise_for_status()
        
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                yield chunk


class TTSServiceFactory:
    PROVIDERS = {
        "google": GoogleTTS,
        "sarvam": SarvamTTS,
        "murf": MurfTTS,
    }
    
    def __init__(self):
        self._instances = {}
    
    def get_service(self, provider_name):
        if provider_name not in self.PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_name}")
        
        if provider_name not in self._instances:
            self._instances[provider_name] = self.PROVIDERS[provider_name]()
        
        return self._instances[provider_name]
    
    def _get_defaults(self):
        """Direct model se fetch.

        🔥 AISettings merge ho gaya Dealer me -- ab provider Dealer.tts_provider
        se aata hai. Dealer me tts_voice field abhi nahi hai (AISettings->Dealer
        merge ke time drop ho gaya) -- isliye voice hardcoded default hi rehta
        hai jab tak wo field wapas nahi aata ya kisi aur source (jaise
        LLMSetting.voice, jo already per-segment TTS voice track karta hai)
        se nahi liya jaata.
        """
        from voice_bot.models import Dealer

        try:
            dealer = Dealer.objects.first()
            if dealer:
                return dealer.tts_provider, "hi-IN-sunaina"
        except Exception as e:
            logger.warning(f"Dealer fetch failed: {e}")

        return "murf", "hi-IN-sunaina"
    
    def synthesize(self, text, provider=None, voice_name=None):
        default_provider, default_voice = self._get_defaults()
        
        provider = provider or default_provider
        voice_name = voice_name or default_voice
        
        service = self.get_service(provider)
        return service.synthesize(text, voice_name)
    
    def synthesize_stream(self, text, provider=None, voice_name=None, cancel_event=None):
        default_provider, default_voice = self._get_defaults()
        
        provider = provider or default_provider
        voice_name = voice_name or default_voice
        
        service = self.get_service(provider)
        
        if not hasattr(service, 'synthesize_stream'):
            raise Exception(f"{provider} doesn't support streaming")
        
        # 🔥 BARGE-IN FIX: pass cancel_event through instead of dropping it.
        # Not every provider's synthesize_stream accepts this kwarg (only
        # MurfTTS does today), so only pass it if the provider supports it --
        # this keeps GoogleTTS/SarvamTTS callers from blowing up.
        import inspect
        try:
            accepts_cancel = 'cancel_event' in inspect.signature(service.synthesize_stream).parameters
        except (TypeError, ValueError):
            accepts_cancel = False

        if accepts_cancel:
            return service.synthesize_stream(text, voice_name, cancel_event=cancel_event)
        return service.synthesize_stream(text, voice_name)


# Singleton
_tts_factory = None

def get_tts_service():
    global _tts_factory
    if _tts_factory is None:
        _tts_factory = TTSServiceFactory()
    return _tts_factory