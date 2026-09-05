"""
Runtime-editable call settings — CALL_TIMEOUT / MAX_CALL_DURATION.

These were hardcoded constants in settings.py with nothing else in the
codebase reading them dynamically. Rather than add a 14th Django model
for two integers, this stores them in Redis (same REDIS_URL already
configured for conversation_history.py's session state) with the
settings.py values as the fallback default if nothing's been saved yet.

IMPORTANT: this file only lets an admin change what's SAVED. Whatever
code currently does `from django.conf import settings; settings.CALL_TIMEOUT`
(most likely dialer.py, which wasn't shared in this conversation) needs to
call get_call_settings() from here instead, or changing this from the
Settings page won't actually affect real calls. Grep your codebase for
CALL_TIMEOUT and MAX_CALL_DURATION usage and swap those call sites.
"""
import json
import logging

import redis
from django.conf import settings

logger = logging.getLogger('recovery_agent')

_REDIS_KEY = "recoverai:call_settings"

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def get_call_settings():
    """Returns {"call_timeout": int, "max_call_duration": int}."""
    defaults = {
        "call_timeout": getattr(settings, "CALL_TIMEOUT", 30),
        "max_call_duration": getattr(settings, "MAX_CALL_DURATION", 600),
    }
    try:
        raw = _get_redis().get(_REDIS_KEY)
        if not raw:
            return defaults
        saved = json.loads(raw)
        return {**defaults, **saved}
    except Exception as e:
        logger.warning(f"[CALL_SETTINGS] read failed, using settings.py defaults: {e}")
        return defaults


def set_call_settings(call_timeout=None, max_call_duration=None):
    current = get_call_settings()
    if call_timeout is not None:
        current["call_timeout"] = int(call_timeout)
    if max_call_duration is not None:
        current["max_call_duration"] = int(max_call_duration)
    try:
        _get_redis().set(_REDIS_KEY, json.dumps(current))
    except Exception as e:
        logger.error(f"[CALL_SETTINGS] save failed: {e}")
        raise
    return current
