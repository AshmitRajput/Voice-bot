from rest_framework.decorators import api_view
from rest_framework.response import Response

from voice_bot.llm.client import GeminiClient


@api_view(["GET"])
def health_check(request):
    """Confirms Django + Redis + LLM client wiring boot correctly."""
    return Response({"status": "ok"})


@api_view(["POST"])
def webhook_incoming_call(request):
    """
    Entry point Exotel hits when a call starts.
    Batch 1: stub only — wires the request through to confirm plumbing works.
    """
    call_sid = request.data.get("call_sid", "unknown")
    return Response({"call_sid": call_sid, "status": "received"})


@api_view(["POST"])
def process_customer_response(request):
    """
    Core per-turn loop: takes customer's spoken text (post-STT),
    builds context, calls the LLM client, returns response text (pre-TTS).
    Batch 1: uses the stub LLM client so the round trip is testable
    before Gemini is wired in.
    """
    customer_text = request.data.get("text", "")
    client = GeminiClient()
    result = client.generate_turn(customer_text=customer_text, context={})
    return Response(result)