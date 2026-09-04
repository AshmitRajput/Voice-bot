"""
tool_registry.py

Single source of truth for LLM-callable tools in the recovery agent.
Every tool (get_recovery_context, update_recovery_case, create_payment_link,
schedule_callback, end_call, ...) registers a ToolSpec here. There is only
ONE registration API now — do not add a second one.
"""

import json
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger('recovery_agent')

# name -> ToolSpec
_REGISTRY: dict = {}
_LOCK = threading.Lock()

# Active session context (set per-request via set_tool_session)
_current_session_id: ContextVar[Optional[str]] = ContextVar(
    "current_tool_session_id", default=None,
)

# Per-session call context (phone, customer name, recovery_case_id, etc.)
_CALL_CONTEXT: dict = {}
_call_context_lock = threading.Lock()

# End-call signaling
_END_CALL_FLAGS: dict = {}
_END_CALL_CALLBACKS: dict = {}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    impl: Callable[[dict], dict]
    modules: set = field(default_factory=lambda: {"*"})
    terminal: bool = False
    prompt_block: Optional[str] = None


# ---------------------------------------------------------------------------
# Session / call context
# ---------------------------------------------------------------------------

def set_tool_session(session_id):
    return _current_session_id.set(str(session_id) if session_id else None)


def reset_tool_session(token):
    _current_session_id.reset(token)


def get_tool_session_id():
    return _current_session_id.get()


def set_call_context(session_id, phone_number=None, customer_name=None, **extra):
    if not session_id:
        return
    with _call_context_lock:
        ctx = {"phone_number": phone_number, "customer_name": customer_name}
        ctx.update(extra)
        _CALL_CONTEXT[str(session_id)] = ctx


def get_call_context(session_id):
    if not session_id:
        return {}
    with _call_context_lock:
        return dict(_CALL_CONTEXT.get(str(session_id), {}))


def clear_call_context(session_id):
    if not session_id:
        return
    with _call_context_lock:
        _CALL_CONTEXT.pop(str(session_id), None)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_tool(spec: ToolSpec, override: bool = False):
    """Register a ToolSpec. Pass override=True to replace an existing tool
    of the same name (used when a *_defs.py module is reloaded)."""
    with _LOCK:
        if spec.name in _REGISTRY and not override:
            logger.warning(
                "tool_registry: '%s' already registered, skipping "
                "(pass override=True to replace)", spec.name,
            )
            return
        _REGISTRY[spec.name] = spec
        logger.debug("tool_registry: registered %s", spec.name)


def get_tool_specs(module=None):
    with _LOCK:
        specs = list(_REGISTRY.values())
    if module is None:
        return specs
    return [s for s in specs if s.modules is None or module in s.modules or "*" in s.modules]


def get_tool_declarations(module=None):
    """Tool specs in a format suitable for an LLM function-calling API."""
    specs = get_tool_specs(module)
    if not specs:
        return None
    return [
        {"name": s.name, "description": s.description, "parameters": s.parameters}
        for s in specs
    ]


def get_tool_prompt_block(module=None):
    """Render all registered tools (+ their detailed prompt_block, if any)
    as text for the system prompt."""
    specs = get_tool_specs(module)
    if not specs:
        return ""
    lines = [
        "You have access to the following tools. When you need to use one, "
        "your ENTIRE reply must be ONLY this JSON:\n",
        '{"tool": "<tool_name>", "arguments": {<arg_name>: <value>, ...}}\n',
        "Available tools:\n",
    ]
    for s in specs:
        lines.append(f"- {s.name}: {s.description}")
        if s.parameters:
            lines.append("  Parameters:")
            for pname, pinfo in s.parameters.items():
                desc = pinfo.get("description", "") if isinstance(pinfo, dict) else str(pinfo)
                lines.append(f"    - {pname}: {desc}")
    for s in specs:
        if s.prompt_block:
            lines.append("\n" + s.prompt_block)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def parse_tool_call(text):
    """Extract a tool call from raw model output."""
    text = (text or "").strip()
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


def execute_tool(name, arguments):
    """Execute a registered tool by name."""
    with _LOCK:
        spec = _REGISTRY.get(name)
    if spec is None:
        return {"error": f"Tool '{name}' not found"}
    if not isinstance(arguments, dict):
        return {"error": "Tool arguments must be a JSON object"}
    try:
        session_id = get_tool_session_id()
        if session_id and "session_id" not in arguments:
            arguments = {**arguments, "session_id": session_id}
        result = spec.impl(arguments)
        return {"result": result, "tool": name, "terminal": spec.terminal}
    except Exception as e:
        logger.exception("execute_tool: %s failed", name)
        return {"error": str(e), "tool": name}


def format_tool_result_for_prompt(tool_name, result):
    if "error" in result:
        return f"[Tool {tool_name} failed: {result['error']}]"
    return f"[Tool {tool_name} result: {json.dumps(result['result'], ensure_ascii=False)}]"


# ---------------------------------------------------------------------------
# End-call signaling
# ---------------------------------------------------------------------------

def register_end_call_handler(session_id, loop, callback):
    with _call_context_lock:
        _END_CALL_CALLBACKS[str(session_id)] = (loop, callback)


def unregister_end_call_handler(session_id):
    with _call_context_lock:
        _END_CALL_CALLBACKS.pop(str(session_id), None)


def mark_call_for_ending(session_id, reason=None):
    if not session_id:
        return
    with _call_context_lock:
        _END_CALL_FLAGS[str(session_id)] = {"reason": reason}
        entry = _END_CALL_CALLBACKS.get(str(session_id))
    if entry:
        loop, callback = entry
        try:
            loop.call_soon_threadsafe(callback, {"reason": reason})
        except Exception:
            logger.exception("mark_call_for_ending: callback dispatch failed")


def should_end_call(session_id):
    if not session_id:
        return None
    with _call_context_lock:
        return _END_CALL_FLAGS.pop(str(session_id), None)