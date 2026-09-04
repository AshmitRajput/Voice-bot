"""
tool_registry.py

Generic infrastructure for registering LLM tools.

Adapted for BharatRouter — no native function calling, tools are embedded in
system prompt as text descriptions.

Recovery Agent domains are loaded here. Honda/service-booking tools are not
part of this project and have intentionally been removed from the registry.
"""

import asyncio
import dataclasses
import importlib
import json
import logging
import threading
from contextvars import ContextVar
from typing import Callable, Optional, Set

logger = logging.getLogger("voice_bot")


# Dotted paths of every domain's *_tool_defs module.
_DOMAIN_MODULES = [
    "recovery_agent.tools.call_control_tools_defs",
    "recovery_agent.tools.callback_tools_defs",
    "recovery_agent.tools.recovery_tools_defs",
]

_REGISTRY: dict = {}
_registry_lock = threading.Lock()

_domains_loaded = False
_domains_loading_lock = threading.Lock()

_current_session_id: ContextVar[Optional[str]] = ContextVar(
    "current_tool_session_id",
    default=None,
)


# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------


def set_tool_session(session_id: Optional[str]):
    """Set the active CallSession for the current execution context."""
    return _current_session_id.set(str(session_id) if session_id else None)


def reset_tool_session(token):
    """Restore the previous tool-session context."""
    _current_session_id.reset(token)


def get_tool_session_id() -> Optional[str]:
    """Return the active CallSession ID for the current tool call."""
    return _current_session_id.get()


_CALL_CONTEXT: dict = {}
_call_context_lock = threading.Lock()


def set_call_context(
    session_id: Optional[str],
    phone_number: Optional[str] = None,
    customer_name: Optional[str] = None,
) -> None:
    if not session_id:
        return
    with _call_context_lock:
        _CALL_CONTEXT[str(session_id)] = {
            "phone_number": phone_number,
            "customer_name": customer_name,
        }


def get_call_context(session_id: Optional[str]) -> dict:
    if not session_id:
        return {}
    with _call_context_lock:
        return dict(_CALL_CONTEXT.get(str(session_id), {}))


def clear_call_context(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with _call_context_lock:
        _CALL_CONTEXT.pop(str(session_id), None)


# ---------------------------------------------------------------------------
# Tool specification / registry
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    impl: Callable[[dict[str, Any]], dict[str, Any]]
    prompt_block: Optional[str] = None
    modules: Optional[set[str]] = None
    terminal: bool = False


def _ensure_domains_loaded():
    """Import every domain module exactly once, on first registry use."""
    global _domains_loaded
    if _domains_loaded:
        return

    with _domains_loading_lock:
        if _domains_loaded:
            return

        _domains_loaded = True
        for mod_path in _DOMAIN_MODULES:
            try:
                importlib.import_module(mod_path)
            except Exception:
                logger.exception(
                    "tool_registry: failed to import domain module %r",
                    mod_path,
                )


def register_tool(spec: ToolSpec, override: bool = False):
    with _registry_lock:
        if not override and spec.name in _REGISTRY:
            raise ValueError(f"tool '{spec.name}' is already registered")
        _REGISTRY[spec.name] = spec
        logger.debug("tool_registry: registered tool %r", spec.name)


def get_tool_specs(module: Optional[str] = None) -> list:
    _ensure_domains_loaded()
    with _registry_lock:
        specs = list(_REGISTRY.values())
    if module is None:
        return specs
    return [s for s in specs if s.modules is None or module in s.modules]


def get_tool_declarations(module: Optional[str] = None):
    """Return tool specs for embedding in the BharatRouter system prompt."""
    specs = get_tool_specs(module)
    if not specs:
        return None
    return specs


def get_tool_impls(module: Optional[str] = None) -> dict:
    return {s.name: s.impl for s in get_tool_specs(module)}


def get_tool_prompt_block(module: Optional[str] = None) -> str:
    specs = get_tool_specs(module)
    blocks = [s.prompt_block for s in specs if s.prompt_block]
    return "\n\n".join(blocks)


def get_tool_names(module: Optional[str] = None) -> list:
    return [s.name for s in get_tool_specs(module)]


def get_tool_spec(name: str) -> Optional[ToolSpec]:
    """Look up a single registered tool by name."""
    _ensure_domains_loaded()
    with _registry_lock:
        return _REGISTRY.get(name)


# ---------------------------------------------------------------------------
# End-call signalling
# ---------------------------------------------------------------------------

_END_CALL_FLAGS: dict = {}
_end_call_lock = threading.Lock()

_END_CALL_CALLBACKS: dict = {}
_end_call_callbacks_lock = threading.Lock()


def register_end_call_handler(
    session_id: str,
    loop: "asyncio.AbstractEventLoop",
    callback,
):
    """Register the consumer callback used when end_call fires."""
    with _end_call_callbacks_lock:
        _END_CALL_CALLBACKS[str(session_id)] = (loop, callback)


def unregister_end_call_handler(session_id: str):
    with _end_call_callbacks_lock:
        _END_CALL_CALLBACKS.pop(str(session_id), None)


def mark_call_for_ending(
    session_id: Optional[str],
    reason: Optional[str] = None,
) -> None:
    if not session_id:
        logger.warning("mark_call_for_ending: called with no session_id, ignoring")
        return

    with _end_call_lock:
        _END_CALL_FLAGS[str(session_id)] = {"reason": reason}

    logger.info(
        "tool_registry: session %s marked for ending (reason=%s)",
        session_id,
        reason,
    )

    with _end_call_callbacks_lock:
        entry = _END_CALL_CALLBACKS.get(str(session_id))

    if entry:
        loop, callback = entry
        loop.call_soon_threadsafe(callback, {"reason": reason})


def should_end_call(session_id: Optional[str]) -> Optional[dict]:
    """Pop and return an end-call signal for a session, if present."""
    if not session_id:
        return None
    with _end_call_lock:
        return _END_CALL_FLAGS.pop(str(session_id), None)


# ---------------------------------------------------------------------------
# Tool parsing / execution
# ---------------------------------------------------------------------------


def parse_tool_call(text: str) -> Optional[tuple[str, dict]]:
    """Extract a tool call from raw model output.

    Expected format:
        {"tool": "<name>", "arguments": {<key>: <value>, ...}}
    """
    text = text.strip()
    if not text.startswith("{"):
        return None

    try:
        data = json.loads(text)
        if isinstance(data, dict) and "tool" in data:
            arguments = data.get("arguments", {})
            if not isinstance(arguments, dict):
                return None
            return data["tool"], arguments
    except Exception:
        pass

    return None


def execute_tool(name: str, arguments: dict) -> dict:
    """Execute a registered tool.

    Returns {"result": ...} on success or {"error": ...} on failure.
    """
    _ensure_domains_loaded()

    with _registry_lock:
        spec = _REGISTRY.get(name)

    if spec is None:
        logger.warning("execute_tool: unknown tool %r", name)
        return {"error": f"Tool '{name}' not found", "tool": name}

    if not isinstance(arguments, dict):
        return {
            "error": "Tool arguments must be a JSON object",
            "tool": name,
        }

    try:
        # The LLM never needs to supply the active session ID itself.
        session_id = get_tool_session_id()
        if session_id and "session_id" not in arguments:
            arguments = {**arguments, "session_id": session_id}

        result = spec.impl(arguments)
        logger.info("execute_tool: %s succeeded", name)
        return {"result": result, "tool": name}
    except Exception as e:
        logger.exception("execute_tool: %s failed", name)
        return {"error": str(e), "tool": name}


def format_tool_result_for_prompt(tool_name: str, result: dict) -> str:
    """Format a tool result for reinjection into the conversation."""
    if "error" in result:
        return f"[Tool {tool_name} failed: {result['error']}]"
    return (
        f"[Tool {tool_name} result: "
        f"{json.dumps(result['result'], ensure_ascii=False)}]"
    )
