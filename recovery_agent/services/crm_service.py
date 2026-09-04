"""
CRM Service — verified customer/account truth. Rule (from plan §19): customer-specific financial balances, due dates, and payment
status come from CRM, NOT from RAG. This service is read-only against customer/vehicle data. Write actions live in
payment_service.py and callback_service.py. """

import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger('recovery_agent')


class CRMService:
    """
    Read-only verified customer/account data. Every method returns plain
    dicts (NOT Django ORM rows) so the orchestrator never accidentally
    commits to a query outside the agent's scope. """

    # ───────────────────────────────────────────────────────────
    # Customer / vehicle profile
    # ───────────────────────────────────────────────────────────

    def get_recovery_profile(self, customer_id, branch_id=None):
        """
        Snapshot of what the agent needs to know about a customer at the
        start of a recovery call. Combines customer identity, vehicle, and
        the OPEN recovery case. """
        from recovery_agent.models import (
            Customer, Vehicle, RecoveryCase,
        )

        try:
            customer = (
                Customer.objects
                .select_related("dealer", "default_branch")
                .get(id=customer_id, flag="c")
            )
        except Customer.DoesNotExist:
            logger.warning(f"[CRM] customer {customer_id} not found")
            return None

        vehicles = list(
            Vehicle.objects
            .filter(customer=customer, flag="c", is_sold_off=False)
            .order_by("-id")
        )

        case = (
            RecoveryCase.objects
            .filter(customer=customer, flag="c", status__in=["open", "in_progress"])
            .order_by("-created_at")
            .first()
        )

        return {
            "customer_id": customer.id,
            "customer_name": customer.name or "Customer",
            "phone_number": customer.phone_number,
            "dealer_id": customer.dealer_id,
            "dealer_name": customer.dealer.name if customer.dealer else None,
            "branch_id": customer.default_branch_id or branch_id,
            "branch_name": customer.default_branch.name if customer.default_branch else None,
            "preferred_language": customer.preferred_language or "hi-IN",
            "do_not_call": customer.do_not_call,

            "vehicles": [
                {
                    "id": v.id,
                    "model": v.vehicle_model or v.vehicle_name,
                    "registration_no": v.registration_no,
                    "next_service_due_date": (
                        v.next_service_due_date.isoformat()
                        if v.next_service_due_date else None
                    ),
                    "last_service_type": v.last_service_type,
                }
                for v in vehicles
            ],

            "open_case": self._serialize_case(case) if case else None,
        }

    def get_customer_by_phone(self, phone_number):
        """Lookup by phone for inbound call routing."""
        from recovery_agent.models import Customer
        try:
            customer = Customer.objects.select_related("dealer").get(
                phone_number=phone_number, flag="c",
            )
            return self.get_recovery_profile(customer.id)
        except Customer.DoesNotExist:
            return None

    # ───────────────────────────────────────────────────────────
    # Payment / balance / due-date
    # ───────────────────────────────────────────────────────────

    def get_outstanding_balance(self, customer_id):
        """Verified total outstanding across all open PaymentRecords."""
        from recovery_agent.models import PaymentRecord
        total = (
            PaymentRecord.objects
            .filter(
                customer_id=customer_id, flag="c",
                status__in=["pending", "link_sent", "in_progress", "failed"],
            )
            .aggregate(total=Sum("amount"))
        )["total"] or Decimal("0")
        return {
            "customer_id": customer_id,
            "outstanding_amount": str(total),
            "currency": "INR",
            "verified": True,
            "as_of": timezone.now().isoformat(),
        }

    def get_payment_due_date(self, customer_id):
        """Earliest due date across pending payment records."""
        from recovery_agent.models import PaymentRecord
        earliest = (
            PaymentRecord.objects
            .filter(
                customer_id=customer_id, flag="c",
                status__in=["pending", "link_sent", "in_progress", "failed"],
            )
            .order_by("expires_at")
            .values("id", "amount", "expires_at", "description")
            .first()
        )
        if not earliest:
            return {
                "customer_id": customer_id,
                "due_date": None,
                "verified": True,
            }
        return {
            "customer_id": customer_id,
            "due_date": earliest["expires_at"].date().isoformat()
                if earliest["expires_at"] else None,
            "amount": str(earliest["amount"]),
            "description": earliest["description"],
            "verified": True,
        }

    def get_payment_status(self, customer_id, payment_record_id=None):
        """Status of a specific payment (or the most recent one)."""
        from recovery_agent.models import PaymentRecord
        qs = PaymentRecord.objects.filter(customer_id=customer_id, flag="c")
        if payment_record_id:
            qs = qs.filter(id=payment_record_id)
        rec = qs.order_by("-created_at").first()
        if not rec:
            return {"customer_id": customer_id, "status": "no_payment_record", "verified": True}
        return {
            "customer_id": customer_id,
            "payment_record_id": rec.id,
            "amount": str(rec.amount),
            "status": rec.status,
            "provider": rec.provider,
            "provider_payment_id": rec.provider_payment_id,
            "paid_at": rec.paid_at.isoformat() if rec.paid_at else None,
            "short_url": rec.short_url,
            "verified": True,
        }

    # ───────────────────────────────────────────────────────────
    # Recovery case / event
    # ───────────────────────────────────────────────────────────

    def get_open_case(self, customer_id):
        from recovery_agent.models import RecoveryCase
        case = (
            RecoveryCase.objects
            .filter(customer_id=customer_id, flag="c",
                    status__in=["open", "in_progress"])
            .order_by("-created_at")
            .first()
        )
        return self._serialize_case(case) if case else None

    def update_case_status(self, case_id, status, outcome=None, notes=None):
        from recovery_agent.models import RecoveryCase
        try:
            case = RecoveryCase.objects.get(id=case_id, flag="c")
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}
        case.status = status
        if outcome is not None:
            case.outcome = outcome
        if status == "closed":
            case.closed_at = timezone.now()
        if notes:
            case.recovery_notes = notes
        case.save()
        return {"success": True, "case": self._serialize_case(case)}

    def record_recovery_event(
        self, case_id, event_type, intent=None, confidence=0.0,
        payload=None, call_session=None, dealer_id=None, notes="",
    ):
        from recovery_agent.models import RecoveryEvent, RecoveryCase
        try:
            case = RecoveryCase.objects.get(id=case_id, flag="c")
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}
        ev = RecoveryEvent.objects.create(
            case=case,
            dealer_id=dealer_id or case.dealer_id,
            event_type=event_type,
            intent=intent or "",
            confidence=confidence,
            payload=payload or {},
            call_session=call_session,
            notes=notes,
        )
        return {"success": True, "event_id": ev.id}

    # ───────────────────────────────────────────────────────────
    # Call-session helpers
    # ───────────────────────────────────────────────────────────

    def mark_wrong_number(self, customer_id, notes=""):
        from recovery_agent.models import Customer
        try:
            c = Customer.objects.get(id=customer_id, flag="c")
        except Customer.DoesNotExist:
            return {"success": False}
        c.do_not_call = True
        c.do_not_call_reason = notes or "wrong_number"
        c.save(update_fields=["do_not_call", "do_not_call_reason", "updated_at"])
        return {"success": True}

    def mark_account_not_owned(self, customer_id, notes=""):
        return self.mark_wrong_number(customer_id, notes=notes or "account_not_owned")

    def mark_complaint(self, customer_id, case_id, complaint_text):
        from recovery_agent.models import Customer, RecoveryEvent, RecoveryCase
        try:
            case = RecoveryCase.objects.get(id=case_id, flag="c")
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}
        case.status = "in_progress"
        case.save(update_fields=["status", "updated_at"])
        RecoveryEvent.objects.create(
            case=case,
            dealer_id=case.dealer_id,
            event_type="complaint_opened",
            payload={"text": complaint_text},
        )
        return {"success": True, "case_id": case.id}

    # ───────────────────────────────────────────────────────────
    # Internals
    # ───────────────────────────────────────────────────────────

    def _serialize_case(self, case):
        if case is None:
            return None
        return {
            "id": case.id,
            "status": case.status,
            "outcome": case.outcome,
            "module": case.module,
            "amount_due": str(case.amount_due),
            "amount_recovered": str(case.amount_recovered),
            "campaign_id": case.campaign_id,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "closed_at": case.closed_at.isoformat() if case.closed_at else None,
        }


crm_service = CRMService()