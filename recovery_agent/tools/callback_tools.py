"""
callback_tools.py

Plain callback-request domain logic for the Recovery Agent.

The implementation is file-backed for the initial buildathon version and can
later be replaced with a Django ORM service without changing the LLM-facing
callback tool definition.
"""

import datetime
import json
import os
import threading
import uuid

CALLBACKS_FILE = os.path.join(os.path.dirname(__file__), "_callbacks_store.json")
_lock = threading.Lock()


def _load_callbacks() -> list:
    if not os.path.exists(CALLBACKS_FILE):
        return []
    try:
        with open(CALLBACKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_callbacks(callbacks: list):
    temp_path = f"{CALLBACKS_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(callbacks, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, CALLBACKS_FILE)


def schedule_callback(
    session_id: str,
    phone_number: str = None,
    customer_name: str = "Customer",
    reason: str = None,
    requested_for: str = None,
) -> dict:
    """Record a customer-requested callback.

    requested_for is optional. If the customer did not give a date/time, it is
    left as None instead of inventing a date. This is important for recovery
    calls because the customer may ask for a callback without specifying when.
    """
    if not session_id:
        raise ValueError("session_id is required")

    if requested_for:
        requested_for = requested_for.strip() or None

    with _lock:
        callbacks = _load_callbacks()
        record = {
            "callback_id": f"cb_{uuid.uuid4().hex[:12]}",
            "session_id": str(session_id),
            "phone_number": phone_number,
            "customer_name": customer_name or "Customer",
            "reason": reason,
            "requested_for": requested_for,
            "status": "pending",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        callbacks.append(record)
        _save_callbacks(callbacks)

    return {"scheduled": True, **record}
