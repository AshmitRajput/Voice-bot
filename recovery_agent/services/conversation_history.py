"""
conversation_history.py — RecoverAI edition. SECTION 1 — conversation history (turn-by-turn transcript log)
Append-only, immutable once written. Fed back into the LLM as history. Stored under key "conversation:{session_id}". SECTION 2 — live call state (per-session mutable state)
What's happening RIGHT NOW in the current turn. Stored under a
separate key, "call_state:{session_id}", so it never collides with
the history above. Now includes recovery-specific state (intent,
recovery_status, payment state, promise, callback, call_outcome). """

import json
import time

from ..utils.redis_client import redis_client


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — conversation history (transcript log)
# ═══════════════════════════════════════════════════════════════

def save_conversation(session_id, speaker, text):
    """Append a conversation turn to Redis."""
    key = f"conversation:{session_id}"
    history = get_conversation_history(session_id)
    history.append({"speaker": speaker, "text": text})
    redis_client.set(key, json.dumps(history))


def get_conversation_history(session_id):
    """Return conversation history as a list."""
    key = f"conversation:{session_id}"
    data = redis_client.get(key)
    if not data:
        return []
    return json.loads(data)


def clear_conversation(session_id):
    """Remove conversation from Redis."""
    redis_client.delete(f"conversation:{session_id}")


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — live call state (mutable per-session)
# ═══════════════════════════════════════════════════════════════

STATE_TTL_SECONDS = 60 * 30


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

        # ─── Recovery-specific state (NEW) ───
        "current_intent": None,
        "intent_confidence": 0.0,
        "intent_entities": {},

        "recovery_status": "pending",
        "last_action": None,
        "last_action_status": None,

        "payment_status": None,            # pending | verified | failed | disputed
        "promise_to_pay": None,           # {"date": "...", "time": "..."} or None
        "payment_link_status": None,      # not_sent | sent | received | failed

        "callback_requested": None,       # {"date": "...", "time": "..."} or None
        "callback_status": None,          # scheduled | confirmed | cancelled

        "call_outcome": None,             # recovered | promise_to_pay | ... (set post-call)
        "complaint_open": False,
    }


def get_state(session_id) -> dict:
    if redis_client is None:
        return _default_state(session_id)
    raw = redis_client.get(_key(session_id))
    if not raw:
        return _default_state(session_id)
    return json.loads(raw)


def _save(session_id, state: dict):
    state["updated_at"] = time.time()
    if redis_client is None:
        return state
    redis_client.set(_key(session_id), json.dumps(state), ex=STATE_TTL_SECONDS)
    return state


def init_state(session_id) -> dict:
    return _save(session_id, _default_state(session_id))


# ───────────────────────────────────────────────────────────────
# Generic low-level mutators (kept from the original file)
# ───────────────────────────────────────────────────────────────

def set_speech_state(session_id, speech_state: str) -> dict:
    state = get_state(session_id)
    state["speech_state"] = speech_state
    state["last_audio_time"] = time.time()
    return _save(session_id, state)


def update_partial_transcript(session_id, text: str) -> dict:
    state = get_state(session_id)
    state["partial_transcript"] = text
    state["revision"] = state.get("revision", 0) + 1
    return _save(session_id, state)


def set_final_transcript(session_id, text: str) -> dict:
    state = get_state(session_id)
    state["final_transcript"] = text
    state["partial_transcript"] = ""
    return _save(session_id, state)


def set_rag_context(session_id, context: str, sources: list = None) -> dict:
    state = get_state(session_id)
    state["current_rag_context"] = context
    state["current_rag_sources"] = sources or []
    return _save(session_id, state)


def set_generating(session_id, is_generating: bool) -> dict:
    state = get_state(session_id)
    state["is_generating"] = is_generating
    return _save(session_id, state)


def clear_state(session_id):
    if redis_client is None:
        return
    redis_client.delete(_key(session_id))


# ───────────────────────────────────────────────────────────────
# Recovery-specific mutators (NEW)
# ───────────────────────────────────────────────────────────────

def set_recovery_intent(session_id, intent: str, confidence: float = 0.0,
                        entities: dict = None) -> dict:
    state = get_state(session_id)
    state["current_intent"] = intent
    state["intent_confidence"] = confidence
    state["intent_entities"] = entities or {}
    return _save(session_id, state)


def get_recovery_intent(session_id) -> dict:
    state = get_state(session_id)
    return {
        "intent": state.get("current_intent"),
        "confidence": state.get("intent_confidence", 0.0),
        "entities": state.get("intent_entities", {}),
    }


def set_recovery_status(session_id, status: str) -> dict:
    state = get_state(session_id)
    state["recovery_status"] = status
    return _save(session_id, state)


def get_recovery_status(session_id) -> str:
    return get_state(session_id).get("recovery_status", "pending")


def set_last_recovery_action(session_id, action: str, status: str = "success") -> dict:
    state = get_state(session_id)
    state["last_action"] = action
    state["last_action_status"] = status
    return _save(session_id, state)


def set_call_outcome(session_id, outcome: str) -> dict:
    state = get_state(session_id)
    state["call_outcome"] = outcome
    return _save(session_id, state)


def get_call_outcome(session_id) -> str:
    return get_state(session_id).get("call_outcome")


def set_payment_state(session_id, payment_status: str) -> dict:
    state = get_state(session_id)
    state["payment_status"] = payment_status
    return _save(session_id, state)


def set_payment_promise(session_id, promise_date: str = None,
                        promise_time: str = None, clear: bool = False) -> dict:
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


def set_payment_link_state(session_id, link_status: str) -> dict:
    """
    link_status: 'not_sent' | 'sent' | 'received' | 'failed' """
    state = get_state(session_id)
    state["payment_link_status"] = link_status
    return _save(session_id, state)


def set_callback_state(session_id, callback_date: str = None,
                       callback_time: str = None, status: str = "scheduled",
                       clear: bool = False) -> dict:
    state = get_state(session_id)
    if clear:
        state["callback_requested"] = None
        state["callback_status"] = None
    else:
        state["callback_requested"] = {
            "date": callback_date,
            "time": callback_time,
        }
        state["callback_status"] = status
    return _save(session_id, state)


def set_complaint_open(session_id, is_open: bool = True) -> dict:
    state = get_state(session_id)
    state["complaint_open"] = is_open
    return _save(session_id, state)