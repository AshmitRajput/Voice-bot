from voice_bot.llm.schemas import TurnResult


class GeminiClient:
    """
    Batch 1: stub implementation.
    Real Gemini API calls + context caching land in Batch 2 (cache_manager.py)
    and Batch 3 (prompt_builder.py) without changing this class's public interface.
    """

    def generate_turn(self, customer_text: str, context: dict) -> dict:
        result = TurnResult(
            intent="stub_intent",
            response_text=f"[stub response] you said: {customer_text}",
            call_status="in_progress",
            summary={},
        )
        return result.model_dump()