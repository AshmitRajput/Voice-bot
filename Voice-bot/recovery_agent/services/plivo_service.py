"""
plivo_service.py — triggers a real outbound Plivo call to a customer and
points it at our WebSocket consumer (dialer.py / PlivoDialerConsumer).

Called from the admin dashboard (a view you wire to a "Call" button) or a
campaign runner. PUBLIC_BASE_URL must be a publicly reachable URL (ngrok
or your real domain) -- Plivo needs to reach both the answer webhook and
the WebSocket from the outside.
"""
import logging
import uuid
import plivo
from django.conf import settings

logger = logging.getLogger('recovery_agent')


def initiate_outbound_call(customer_id):
    """Places a real Plivo call to the given Customer. Returns
    {"success": bool, "session_id": str, "call_uuid": str|None, "error": str|None}"""
    from recovery_agent.models import Customer

    try:
        customer = Customer.objects.get(id=customer_id, flag='c')
    except Customer.DoesNotExist:
        return {"success": False, "error": "customer not found"}

    if customer.do_not_call:
        return {"success": False, "error": "customer is on do_not_call list"}

    session_id = str(uuid.uuid4())
    base = settings.PUBLIC_BASE_URL.rstrip('/')
    ws_scheme = "wss" if base.startswith("https") else "ws"
    ws_host = base.split("://", 1)[1]

    answer_url = f"{base}/api/voice/plivo/answer/?session_id={session_id}&phone={customer.phone_number}"
    hangup_url = f"{base}/api/voice/plivo/hangup/?session_id={session_id}"

    try:
        client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)
        response = client.calls.create(
            from_=settings.PLIVO_FROM_NUMBER,
            to_=customer.phone_number,
            answer_url=answer_url,
            answer_method='GET',
            hangup_url=hangup_url,
            hangup_method='GET',
        )
        logger.info(f"[PLIVO] call initiated: customer_id={customer_id} "
                    f"session_id={session_id} call_uuid={response.request_uuid}")
        return {"success": True, "session_id": session_id, "call_uuid": response.request_uuid, "error": None}
    except Exception as e:
        logger.error(f"[PLIVO] initiate call failed for customer_id={customer_id}: {e}")
        return {"success": False, "session_id": session_id, "call_uuid": None, "error": str(e)}