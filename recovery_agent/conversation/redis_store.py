import json
from typing import Any

from recovery_agent.utils.redis_client import redis_client


def get_turns(call_id: str) -> list[dict[str, str]]:
    """
    Return all conversation turns for a call.

    Redis key:
        call:{call_id}:turns
    """

    if redis_client is None:
        return []

    raw = redis_client.get(
        f"call:{call_id}:turns"
    )

    if not raw:
        return []

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    return [
        item
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("role"), str)
        and isinstance(item.get("text"), str)
    ]


def append_turn(
    call_id: str,
    role: str,
    text: str,
) -> None:
    """
    Append a conversation turn to Redis.
    """

    if redis_client is None:
        return

    turns = get_turns(call_id)

    turns.append(
        {
            "role": role,
            "text": text,
        }
    )

    redis_client.set(
        f"call:{call_id}:turns",
        json.dumps(
            turns,
            ensure_ascii=False,
        ),
    )


def clear_turns(call_id: str) -> None:
    """
    Delete all conversation turns for a call.
    """

    if redis_client is None:
        return

    redis_client.delete(
        f"call:{call_id}:turns"
    )