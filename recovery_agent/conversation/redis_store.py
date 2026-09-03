import json
import redis
from django.conf import settings

_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def get_turns(call_sid: str) -> list:
    raw = _client.get(f"call:{call_sid}:turns")
    return json.loads(raw) if raw else []


def append_turn(call_sid: str, role: str, text: str) -> None:
    turns = get_turns(call_sid)
    turns.append({"role": role, "text": text})
    _client.set(f"call:{call_sid}:turns", json.dumps(turns))


def clear_turns(call_sid: str) -> None:
    _client.delete(f"call:{call_sid}:turns")