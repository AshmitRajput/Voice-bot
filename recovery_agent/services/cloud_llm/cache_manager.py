"""
Vertex AI / Gemini Enterprise Agent Platform context-cache manager.

Caches the exact persona text and, for streaming turns, the exact tool
declarations used by that module.

Why tools are included in the cache:
Agent Platform context-cache requests must not resend system instructions or
tools that were already specified when the cache was created. Therefore a
persona-only cache cannot safely be reused for a request that also sends tools.

Why location/project are included in the cache hash:
Cached-content resources are region-scoped (and project-scoped). A cache
created while GCP_LOCATION="europe-west1" does not exist in
GCP_LOCATION="asia-south1" -- reusing a Redis-registered cache name across a
location swap produces a 404 NOT_FOUND at generate time. Hashing the location
(and project) in means each region gets its own cache entry instead of
colliding on the same persona/tools hash.

Redis remains the local cache-name registry. Redis does NOT contain the actual
Gemini context; Google Cloud owns the cached content resource.
"""

import hashlib
import json

from django.conf import settings
from google import genai
from google.genai import types
from google.oauth2 import service_account

from ...utils.redis_client import redis_client as _redis


_UNAVAILABLE_KEY = "vertex:persona_cache_unavailable"
_UNAVAILABLE_REASON_KEY = "vertex:persona_cache_unavailable_reason"

_TRANSIENT_RETRY_SECONDS = 86400
_PERMANENT_RETRY_SECONDS = 86400

_client = None


def _get_client():
    global _client
    if _client is None:
        credentials = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_APPLICATION_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _client = genai.Client(
            vertexai=True,
            project=settings.GCP_PROJECT_ID,
            location=settings.GCP_LOCATION,
            credentials=credentials,
        )
    return _client

def _create_cache_sync(persona_text, tools, cache_hash, cache_key):
    """The existing try/except client.caches.create(...) body — unchanged,
    just pulled out so both the blocking and background paths can call it."""
    client = _get_client()

    try:
        config = types.CreateCachedContentConfig(
            system_instruction=persona_text,
            ttl=f"{settings.GEMINI_CACHE_TTL_SECONDS}s",
        )

        if tools:
            config.tools = tools

        cache = client.caches.create(
            model=settings.GEMINI_MODEL,
            config=config,
        )

    except Exception as e:
        message = str(e)

        cooldown = (
            _PERMANENT_RETRY_SECONDS
            if _is_permanent_failure(message)
            else _TRANSIENT_RETRY_SECONDS
        )

        print(
            f"[VERTEX-CACHE] FAILED hash={cache_hash} "
            f"reason='{message}' -- fallback to uncached request; "
            f"retrying cache creation in {cooldown}s"
        )

        _redis.set(
            _UNAVAILABLE_KEY,
            "1",
            ex=cooldown,
        )
        _redis.set(
            _UNAVAILABLE_REASON_KEY,
            message,
            ex=cooldown,
        )

        return None

    _redis.set(
        cache_key,
        cache.name,
        ex=settings.GEMINI_CACHE_TTL_SECONDS,
    )

    print(
        f"[VERTEX-CACHE] CREATED hash={cache_hash} "
        f"cache_name={cache.name} "
        f"ttl={settings.GEMINI_CACHE_TTL_SECONDS}s "
        f"tools_cached={bool(tools)}"
    )

    return cache.name


def _is_permanent_failure(error_text: str) -> bool:
    lowered = error_text.lower()
    return (
        "limit=0" in lowered
        or "billing" in lowered
        or "permission denied" in lowered
        or "permission_denied" in lowered
    )


def _serialize_tool(tool) -> dict:
    """
    Convert google.genai Tool objects into deterministic JSON.

    Pydantic models expose model_dump(); the fallback handles older SDK
    versions that may expose dict().
    """
    if hasattr(tool, "model_dump"):
        return tool.model_dump(mode="json", exclude_none=True)

    if hasattr(tool, "dict"):
        return tool.dict(exclude_none=True)

    if isinstance(tool, dict):
        return tool

    return {"repr": repr(tool)}


def _build_cache_hash(persona_text: str, tools=None) -> str:
    payload = {
        "persona": persona_text,
        "tools": [
            _serialize_tool(tool)
            for tool in (tools or [])
        ],
        "location": settings.GCP_LOCATION,
        "project": settings.GCP_PROJECT_ID,
    }

    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:16]


def get_or_create_persona_cache(persona_text: str, tools=None, blocking=True):
    """
    Return a Google Cloud cached-content resource name.

    `tools=None` means persona-only cache.
    `tools=[...]` means persona + tool declarations are cached together.

    Callers must NOT send system_instruction/tools again when the returned
    cache_name is used.
    """
    cache_hash = _build_cache_hash(persona_text, tools)

    if _redis is None:
        print(
            f"[VERTEX-CACHE] SKIPPED hash={cache_hash} "
            "reason='redis unavailable (REDIS_ENABLED=False)'"
        )
        return None

    cache_key = f"vertex:persona_cache_name:{cache_hash}"

    existing = _redis.get(cache_key)
    if existing:
        print(
            f"[VERTEX-CACHE] REUSE hash={cache_hash} "
            f"cache_name={existing}"
        )
        return existing

    unavailable = _redis.get(_UNAVAILABLE_KEY)

    if unavailable:
        cooldown_ttl = _redis.ttl(_UNAVAILABLE_KEY)
        reason = (
            _redis.get(_UNAVAILABLE_REASON_KEY)
            or "unknown"
        )
        print(
            f"[VERTEX-CACHE] SKIPPED hash={cache_hash} "
            f"reason='cooldown ({cooldown_ttl}s left), "
            f"last failure: {reason}'"
        )
        return None

    if not blocking:
        import threading
        print(f"[VERTEX-CACHE] MISS hash={cache_hash} -- creating in background, this turn goes uncached")
        threading.Thread(
            target=_create_cache_sync,
            args=(persona_text, tools, cache_hash, cache_key),
            daemon=True,
        ).start()
        return None

    return _create_cache_sync(persona_text, tools, cache_hash, cache_key)


def evict_persona_cache(persona_text: str, tools=None):
    """
    Remove the Redis registry entry for this persona/tools/location
    combination without touching the underlying Google Cloud resource
    (which either already expired or 404s on its own).

    Call this when a generate request comes back with a "cached content
    not found" error for a cache_name that Redis still thinks is valid --
    e.g. the cache's server-side TTL lapsed slightly ahead of Redis's TTL,
    or (pre location-hash-fix) a stale cross-region name was reused.
    """
    if _redis is None:
        return

    cache_hash = _build_cache_hash(persona_text, tools)
    cache_key = f"vertex:persona_cache_name:{cache_hash}"

    removed = _redis.delete(cache_key)

    print(
        f"[VERTEX-CACHE] EVICT hash={cache_hash} "
        f"cache_key={cache_key} removed={bool(removed)}"
    )


def invalidate_all_persona_caches():
    """
    Clear only the local failure cooldown.

    Existing Google Cloud cached-content resources naturally expire according
    to their TTL. Redis cache-name entries also expire with the same TTL.
    """
    if _redis is None:
        return

    _redis.delete(_UNAVAILABLE_KEY)
    _redis.delete(_UNAVAILABLE_REASON_KEY)


def cache_status(persona_text: str, tools=None) -> dict:
    cache_hash = _build_cache_hash(persona_text, tools)

    if _redis is None:
        return {
            "persona_hash": cache_hash,
            "cache_name": None,
            "unavailable_cooldown_active": False,
            "tools_cached": bool(tools),
        }

    return {
        "persona_hash": cache_hash,
        "cache_name": _redis.get(
            f"vertex:persona_cache_name:{cache_hash}"
        ),
        "unavailable_cooldown_active": bool(
            _redis.get(_UNAVAILABLE_KEY)
        ),
        "tools_cached": bool(tools),
    }