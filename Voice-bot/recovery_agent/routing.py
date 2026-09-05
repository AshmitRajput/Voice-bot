from django.urls import re_path
from . import consumers, dialer

websocket_urlpatterns = [
    # Browser/Web client
    re_path(r'api/voice/ws/audio$', consumers.VoiceChatConsumer.as_asgi()),

    # Plivo dialer -- session_id/phone come from the URL Plivo's Stream
    # verb dials, which views.plivo_answer() builds.
    re_path(
        r'api/voice/ws/plivo/(?P<session_id>[^/]+)(?:/(?P<phone>[^/]+))?/?$',
        dialer.PlivoDialerConsumer.as_asgi()
    ),
]