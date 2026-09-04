"""
recovery_agent.tools package.

Importing this package registers every recovery tool exactly once
(get_recovery_context, update_recovery_case, create_payment_link,
schedule_callback, end_call) and re-exports the handful of helpers used
by recovery_service.py and consumers.py.
"""

from recovery_agent.tools.tool_registry import (
    ToolSpec,
    register_tool,
    get_tool_specs,
    get_tool_declarations,
    get_tool_prompt_block,
    parse_tool_call,
    execute_tool,
    format_tool_result_for_prompt,
    set_tool_session,
    reset_tool_session,
    get_tool_session_id,
    set_call_context,
    get_call_context,
    clear_call_context,
    mark_call_for_ending,
    should_end_call,
    register_end_call_handler,
    unregister_end_call_handler,
)

# Import side-effects: each *_defs module registers its own tool(s) at
# import time. Order doesn't matter for registration, but keep it stable.
from recovery_agent.tools import recovery_tools_defs   # noqa: F401
from recovery_agent.tools import callback_tools_defs    # noqa: F401
from recovery_agent.tools import call_control_tools_defs  # noqa: F401

__all__ = [
    "ToolSpec", "register_tool", "get_tool_specs", "get_tool_declarations",
    "get_tool_prompt_block", "parse_tool_call", "execute_tool",
    "format_tool_result_for_prompt", "set_tool_session", "reset_tool_session",
    "get_tool_session_id", "set_call_context", "get_call_context",
    "clear_call_context", "mark_call_for_ending", "should_end_call",
    "register_end_call_handler", "unregister_end_call_handler",
]