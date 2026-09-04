"""
Cloud LLM service layer — RecoverAI (BharatRouter / Gemma 4 31B) edition.
"""

import logging

from .cloud_llm.schemas import TurnResult, LiveTurnResult
from .cloud_llm import BharatRouterClient
from .cloud_llm.prompt_builder import get_persona_instruction
from .rag_service import get_rag_service
from recovery_agent.tools import tool_registry

logger = logging.getLogger("voice_bot")

_client = None


def get_cloud_llm_client():
    global _client
    if _client is None:
        _client = BharatRouterClient()
    return _client


# ═══════════════════════════════════════════════════════════════
# RAG RESOLUTION — category-scoped, no dealer/branch
# ═══════════════════════════════════════════════════════════════

def _get_rag_reference(category, customer_text, top_k=3):
    try:
        rag = get_rag_service()
        result = rag.ask_question(category, customer_text, top_k=top_k)

        logger.debug(
            "[RAG] category=%s query=%r -> success=%s n_contexts=%d "
            "best_distance=%s timing=%s",
            category, customer_text, result.get("success"),
            len(result.get("contexts", [])),
            result.get("best_distance"), result.get("timing_ms"),
        )

        contexts = result.get("contexts", [])
        if not contexts:
            logger.debug("[RAG] no contexts returned for query=%r", customer_text)
            return None

        return "\n\n".join(contexts)

    except Exception as e:
        logger.error(f"cloud_llm_service RAG lookup failed: {e}", exc_info=True)
        return None


def _resolve_reference(customer_text, context, use_rag, reference_context):
    if not use_rag:
        return reference_context

    category = context.get("rag_category", "communication_policy")
    rag_context = _get_rag_reference(category, customer_text)

    if rag_context and reference_context:
        return f"{reference_context}\n\n{rag_context}"
    if rag_context:
        return rag_context
    return reference_context


# ═══════════════════════════════════════════════════════════════
# USAGE / PRICING
# ═══════════════════════════════════════════════════════════════

def _extract_usage(client) -> dict:
    return {
        "prompt_tokens":  getattr(client, "last_prompt_token_count",  None),
        "output_tokens":  getattr(client, "last_output_token_count",  None),
        "total_tokens":   getattr(client, "last_total_token_count",   None),
        "cached_tokens":  getattr(client, "last_cached_token_count",  None),
        "cache_hit":      getattr(client, "last_cache_hit",           None),
        "latency_seconds": getattr(client, "last_latency_seconds",    None),
    }


LLM_PRICE_PER_MTOK_IN = 9
LLM_PRICE_PER_MTOK_OUT = 33


def _calculate_llm_pricing(prompt_tokens, output_tokens) -> float:
    return (
        (prompt_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_IN
        + (output_tokens or 0) / 1_000_000 * LLM_PRICE_PER_MTOK_OUT
    )


# ═══════════════════════════════════════════════════════════════
# WARMUP
# ═══════════════════════════════════════════════════════════════

def warmup_module(workflow: str = "revenue_recovery"):
    """Fire a throwaway generate_turn_stream() call during greeting
    playback so the customer's first utterance doesn't pay connection
    warm-up cost."""
    client = get_cloud_llm_client()
    dummy_context = {
        "customer_name": "Warmup",
        "outstanding_amount": "0",
        "due_date": "warmup",
        "workflow": workflow,
        "today": "warmup",
        "current_datetime_ist": "warmup",
    }
    try:
        for _ in client.generate_turn_stream(
            customer_text="नमस्ते",
            context=dummy_context,
            history=[],
            tools=None,
        ):
            pass
        print(f"✅ [WARMUP] connection primed for workflow={workflow}")
    except Exception as e:
        print(f"⚠️ [WARMUP] failed for workflow={workflow}: {e}")


# ═══════════════════════════════════════════════════════════════
# PUBLIC TURN ENTRY POINTS
# ═══════════════════════════════════════════════════════════════

def chat_turn(
    session_id, customer_text, context, history=None, use_rag=True,
    reference_context=None, filler_text=None, interrupted_context=None,
):
    """Non-streaming turn."""
    client = get_cloud_llm_client()
    reference_context = _resolve_reference(customer_text, context, use_rag, reference_context)
    tool_session_token = tool_registry.set_tool_session(session_id)
    try:
        result = client.generate_turn(
            customer_text=customer_text,
            context=context,
            history=history or [],
            reference_context=reference_context,
            filler_text=filler_text,
            interrupted_context=interrupted_context,
        )
    finally:
        tool_registry.reset_tool_session(tool_session_token)
    result["usage"] = _extract_usage(client)
    return result


def chat_turn_stream(
    session_id, customer_text, context, history=None, use_rag=True,
    reference_context=None, filler_text=None, interrupted_context=None,
):
    """Streaming voice-bot path."""
    client = get_cloud_llm_client()
    workflow = context.get("workflow", "revenue_recovery")
    tools = tool_registry.get_tool_declarations(workflow)
    reference_context = _resolve_reference(customer_text, context, use_rag, reference_context)
    tool_session_token = tool_registry.set_tool_session(session_id)
    try:
        for chunk in client.generate_turn_stream(
            customer_text=customer_text,
            context=context,
            history=history or [],
            reference_context=reference_context,
            filler_text=filler_text,
            tools=tools,
            interrupted_context=interrupted_context,
        ):
            yield chunk
    finally:
        tool_registry.reset_tool_session(tool_session_token)


def get_last_turn_usage() -> dict:
    client = get_cloud_llm_client()
    return _extract_usage(client)


# ═══════════════════════════════════════════════════════════════
# POST-TURN SCORING
# ═══════════════════════════════════════════════════════════════

def score_and_price_turn(customer_text, bot_response_text, filler_text,
                          turn_prompt_tokens, turn_output_tokens):
    client = get_cloud_llm_client()
    result = client.score_turn(
        customer_text=customer_text,
        bot_response_text=bot_response_text,
        filler_text=filler_text,
        turn_prompt_tokens=turn_prompt_tokens,
        turn_output_tokens=turn_output_tokens,
    )
    result["llm_pricing"] = _calculate_llm_pricing(turn_prompt_tokens, turn_output_tokens)
    return result


# ═══════════════════════════════════════════════════════════════
# TIMING
# ═══════════════════════════════════════════════════════════════

def build_timing_record(timing: dict) -> dict:
    timing = timing or {}

    def ms(key_end, key_start):
        end = timing.get(key_end)
        start = timing.get(key_start)
        if end is None or start is None:
            return None
        return round((end - start) * 1000, 1)

    return {
        "stt_first_token": timing.get("stt_first_token_ms"),
        "stt_complete":     ms("stt_done_at", "audio_received_at"),
        "filler_play":      ms("filler_first_chunk_at", "filler_requested_at"),
        "llm_first_token":  ms("llm_first_token_at", "stt_done_at"),
        "tts_first_token":  timing.get("tts_first_audio_ms"),
        "llm_complete":     ms("llm_complete_at", "llm_first_token_at"),
        "filler_audio_at_ms":   ms("filler_first_chunk_at", "audio_received_at"),
        "response_audio_at_ms": ms("real_user_heard_at", "audio_received_at"),
    }