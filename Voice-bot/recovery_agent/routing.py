from django.urls import re_path
from . import consumers, dialer, consumers_sip

websocket_urlpatterns = [
    # Browser/Web client ke liye
    re_path(r'api/voice/ws/audio$', consumers.VoiceChatConsumer.as_asgi()),

    # Plivo dialer ke liye -- session_id/phone URL me hi embedded hain
    # (outbound: plivo_call() ne answer_url me daale, plivo_answer() ne
    # WS URL me carry kiya; inbound/raw test client: dono absent, connect()
    # generic default use karta hai). Same idiom jo sip_audio neeche
    # already use karta hai.
    re_path(
        r'api/voice/ws/plivo/(?P<session_id>[^/]+)(?:/(?P<phone>[^/]+))?/?$',
        dialer.PlivoDialerConsumer.as_asgi()
    ),

    # FreeSWITCH SIP trunk outbound dialer ke liye (mod_audio_fork target).
    re_path(
        r'api/voice/ws/sip_audio/(?P<call_uuid>[^/]+)(?:/(?P<phone>[^/]+))?$',
        consumers_sip.SIPAudioConsumer.as_asgi()
    ),
]