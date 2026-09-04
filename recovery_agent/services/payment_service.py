"""
services/payment_service.py

Per your plan doc (section 14), this is where real payment-provider
integration (Razorpay/etc.) belongs. This is a MINIMAL STUB so
create_payment_link has something real to call -- it does NOT talk to a
payment provider yet. Replace generate_payment_link() before this reaches
production; until then it stores an obviously-fake placeholder URL so
nobody mistakes it for a working link, and the LLM-facing tool result
already tells the agent never to claim payment succeeded from this alone.

Functions your plan doc lists that still need real implementations here:
    get_payment_status()
    get_outstanding_balance()
    get_due_date()
    verify_payment()
    generate_payment_link()   <- stubbed below
    resend_payment_link()
    record_payment_event()
"""

import logging
import uuid

logger = logging.getLogger("recovery_agent")


class PaymentService:
    def generate_payment_link(self, payment_id, channel: str = "whatsapp") -> str:
        # PLACEHOLDER -- replace with real provider call (e.g. Razorpay
        # Payment Links API) before any real customer sees this.
        token = uuid.uuid4().hex[:10]
        link = f"https://pay.example-placeholder.com/{payment_id}/{token}"
        logger.warning(
            "payment_service.generate_payment_link: STUB link generated for "
            "payment_id=%s channel=%s -- wire up real provider before prod",
            payment_id, channel,
        )
        return link

    def verify_payment(self, payment_id) -> dict:
        raise NotImplementedError(
            "verify_payment: wire up real provider/CRM lookup before use"
        )


payment_service = PaymentService()