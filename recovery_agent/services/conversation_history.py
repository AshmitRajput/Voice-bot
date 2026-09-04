"""
conversation_history.py — RecoverAI edition. SECTION 1 — conversation history (turn-by-turn transcript log)
Append-only, immutable once written. SECTION 2 — live call state (per-session mutable state) """

import json
import time

from django.conf import settings


# ═══════════════════════════════════════════════════════════════
# REDIS CLIENT (lazy init, falls back to in-memory if Redis is down)
# ═══════════════════════════════════════════════════════════════

_redis_client = None
_redis_checked = False


def _get_redis():
    """Lazy-init Redis. Returns None if Redis is not configured or unavailable."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis
        url = getattr(settings, "REDIS_URL", None)
        if not url:
            return None
        _redis_client = redis.from_url(url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning(f"[REDIS] not available, using in-memory fallback: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# In-memory fallback (so the app works without Redis)
# ═══════════════════════════════════════════════════════════════

import threading
import logging

logger = logging.getLogger('recovery_agent')

_MEM_HISTORY = {}
_MEM_STATE = {}
_LOCK = threading.Lock()

STATE_TTL_SECONDS = 60 * 30  # 30 minutes


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — conversation history (transcript log)
# ═══════════════════════════════════════════════════════════════

def save_conversation(session_id, speaker, text):
    """Append a conversation turn."""
    history = get_conversation_history(session_id)
    history.append({"speaker": speaker, "text": text})
    rc = _get_redis()
    if rc is not None:
        try:
            rc.set(f"conversation:{session_id}", json.dumps(history))
            return
        except Exception:
            pass
    with _LOCK:
        _MEM_HISTORY[session_id] = history


def get_conversation_history(session_id):
    rc = _get_redis()
    if rc is not None:
        try:
            data = rc.get(f"conversation:{session_id}")
            if data:
                return json.loads(data)
        except Exception:
            pass
    with _LOCK:
        return list(_MEM_HISTORY.get(session_id, []))


def clear_conversation(session_id):
    rc = _get_redis()
    if rc is not None:
        try:
            rc.delete(f"conversation:{session_id}")
        except Exception:
            pass
    with _LOCK:
        _MEM_HISTORY.pop(session_id, None)


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — live call state (mutable per-session)
# ═══════════════════════════════════════════════════════════════

def _key(session_id):
    return f"call_state:{session_id}"


def _default_state(session_id):
    return {
        "session_id": session_id,
        "speech_state": "idle",
        "partial_transcript": "",
        "final_transcript": "",
        "current_rag_context": None,
        "current_rag_sources": [],
        "is_generating": False,
        "revision": 0,
        "last_audio_time": None,
        "updated_at": time.time(),

        "current_intent": None,
        "intent_confidence": 0.0,
        "intent_entities": {},

        "recovery_status": "pending",
        "last_action": None,
        "last_action_status": None,

        "payment_status": None,
        "promise_to_pay": None,
        "payment_link_status": None,

        "callback_requested": None,
        "callback_status": None,

        "call_outcome": None,
        "complaint_open": False,
    }


def get_state(session_id):
    rc = _get_redis()
    if rc is not None:
        try:
            data = rc.get(_key(session_id))
            if data:
                return json.loads(data)
        except Exception:
            pass
    with _LOCK:
        return dict(_MEM_STATE.get(session_id) or _default_state(session_id))


def _save(session_id, state):
    state["updated_at"] = time.time()
    rc = _get_redis()
    if rc is not None:
        try:
            rc.set(_key(session_id), json.dumps(state), ex=STATE_TTL_SECONDS)
            return state
        except Exception:
            pass
    with _LOCK:
        _MEM_STATE[session_id] = state
    return state


def init_state(session_id):
    return _save(session_id, _default_state(session_id))


def clear_state(session_id):
    rc = _get_redis()
    if rc is not None:
        try:
            rc.delete(_key(session_id))
        except Exception:
            pass
    with _LOCK:
        _MEM_STATE.pop(session_id, None)


# Generic low-level mutators (kept from original file)
def set_speech_state(session_id, speech_state):
    state = get_state(session_id)
    state["speech_state"] = speech_state
    state["last_audio_time"] = time.time()
    return _save(session_id, state)


def update_partial_transcript(session_id, text):
    state = get_state(session_id)
    state["partial_transcript"] = text
    state["revision"] = state.get("revision", 0) + 1
    return _save(session_id, state)


def set_final_transcript(session_id, text):
    state = get_state(session_id)
    state["final_transcript"] = text
    state["partial_transcript"] = ""
    return _save(session_id, state)


def set_rag_context(session_id, context, sources=None):
    state = get_state(session_id)
    state["current_rag_context"] = context
    state["current_rag_sources"] = sources or []
    return _save(session_id, state)


def set_generating(session_id, is_generating):
    state = get_state(session_id)
    state["is_generating"] = is_generating
    return _save(session_id, state)


# Recovery-specific mutators
def set_recovery_intent(session_id, intent, confidence=0.0, entities=None):
    state = get_state(session_id)
    state["current_intent"] = intent
    state["intent_confidence"] = confidence
    state["intent_entities"] = entities or {}
    return _save(session_id, state)


def get_recovery_intent(session_id):
    state = get_state(session_id)
    return {
        "intent": state.get("current_intent"),
        "confidence": state.get("intent_confidence", 0.0),
        "entities": state.get("intent_entities", {}),
    }


def set_recovery_status(session_id, status):
    state = get_state(session_id)
    state["recovery_status"] = status
    return _save(session_id, state)


def get_recovery_status(session_id):
    return get_state(session_id).get("recovery_status", "pending")


def set_last_recovery_action(session_id, action, status="success"):
    state = get_state(session_id)
    state["last_action"] = action
    state["last_action_status"] = status
    return _save(session_id, state)


def set_call_outcome(session_id, outcome):
    state = get_state(session_id)
    state["call_outcome"] = outcome
    return _save(session_id, state)


def get_call_outcome(session_id):
    return get_state(session_id).get("call_outcome")


def set_payment_state(session_id, payment_status):
    state = get_state(session_id)
    state["payment_status"] = payment_status
    return _save(session_id, state)


def set_payment_promise(session_id, promise_date=None, promise_time=None, clear=False):
    state = get_state(session_id)
    if clear or not promise_date:
        state["promise_to_pay"] = None
    else:
        state["promise_to_pay"] = {
            "date": promise_date,
            "time": promise_time,
            "recorded_at": time.time(),
        }
    return _save(session_id, state)


def set_payment_link_state(session_id, link_status):
    state = get_state(session_id)
    state["payment_link_status"] = link_status
    return _save(session_id, state)


def set_callback_state(session_id, callback_date=None, callback_time=None,
                       status="scheduled", clear=False):
    state = get_state(session_id)
    if clear:
        state["callback_requested"] = None
        state["callback_status"] = None
    else:
        state["callback_requested"] = {"date": callback_date, "time": callback_time}
        state["callback_status"] = status
    return _save(session_id, state)


def set_complaint_open(session_id, is_open=True):
    state = get_state(session_id)
    state["complaint_open"] = is_open
    return _save(session_id, state)