#!/bin/bash
# Quick curl tests for the recovery agent. Run: bash scripts/curl_tests.sh

BASE="http://localhost:8000/api"

echo "════════════════════════════════════════════"
echo "1. Health check"
echo "════════════════════════════════════════════"
curl -s "$BASE/health/" | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "2. List all tools"
echo "════════════════════════════════════════════"
curl -s "$BASE/test/list-tools/" | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "3. Classify intent (no LLM needed - keyword fallback)"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/classify-intent/" \
  -H "Content-Type: application/json" \
  -d '{"customer_text": "Maine payment kar diya hai"}' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "4. Process a turn (needs GOOGLE_API_KEY)"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/process-turn/" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-curl-1",
    "customer_id": 1,
    "customer_text": "Mera payment pending hai kya?"
  }' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "5. Process a promise_to_pay turn"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/process-turn/" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-curl-2",
    "customer_id": 1,
    "customer_text": "15 tarikh ko payment kar dunga",
    "history": [
      {"role": "customer", "text": "Mera kitna pending hai?"},
      {"role": "assistant", "text": "Aapka 2500 rupaye pending hai."}
    ]
  }' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "6. Tool call directly - send_payment_link"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/tool-call/" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "send_payment_link",
    "arguments": {"customer_id": 1, "session_id": "test-curl-3"}
  }' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "7. Tool call - verify_payment"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/tool-call/" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "verify_payment",
    "arguments": {"customer_id": 1, "session_id": "test-curl-3"}
  }' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "8. Tool call - schedule_callback"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/tool-call/" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "schedule_callback",
    "arguments": {
      "customer_id": 1,
      "session_id": "test-curl-3",
      "callback_date": "2026-09-20",
      "callback_time": "10:00",
      "reason": "customer requested"
    }
  }' | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "9. Get session state"
echo "════════════════════════════════════════════"
curl -s "$BASE/test/state/?session_id=test-curl-3&customer_id=1" | python3 -m json.tool

echo ""
echo "════════════════════════════════════════════"
echo "10. Clear session"
echo "════════════════════════════════════════════"
curl -s -X POST "$BASE/test/clear-session/" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-curl-1"}' | python3 -m json.tool

echo ""
echo "✅ All tests done!"