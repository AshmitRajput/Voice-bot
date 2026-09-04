"""
CRM Service — verified customer/account truth.

Rule (plan doc §19 / §4): customer-specific balances, due dates, and
payment status come from the DB (Customer/RecoveryCase/PaymentRecord),
NEVER from RAG, and NEVER from customer speech alone. This service is
read-only against customer/case/payment data. Writes to payment state
live in payment_service.py; writes to callback state live in
callback_service.py. This service DOES write RecoveryCase/RecoveryEvent
rows (case status + audit trail), because that's recovery-process state,
not financial truth.

Rewritten against the real models.py (13-model RecoverAI schema).
Removed entirely: Vehicle, Dealer, Branch, customer.flag="c" bug
(real Customer/RecoveryCase use LogicalDeleteMixin's flag='c', which is
correct — the bug in the old version was querying on a field named
"flag" with the string "c", which IS right; what was wrong was that it
also filtered/joined on dealer/branch/vehicle fields that don't exist
on the real model. Those are removed below.)
"""

import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger('recovery_agent')


class CRMService:
    """
    Read-only verified customer/case/payment data, plus the narrow set of
    case-state writes (status/outcome/events) that belong to the recovery
    process itself. Every method returns plain dicts (not ORM rows) so
    callers (tools, recovery_service) never hold a live queryset past the
    call.
    """

    # ───────────────────────────────────────────────────────────
    # Customer / case profile
    # ───────────────────────────────────────────────────────────

    def get_recovery_profile(self, customer_id):
        """
        Snapshot of what the agent needs at the start of a recovery call:
        customer identity + the open RecoveryCase (if any).
        """
        from recovery_agent.models import Customer, RecoveryCase

        try:
            customer = Customer.objects.get(id=customer_id, flag='c')
        except Customer.DoesNotExist:
            logger.warning(f"[CRM] customer {customer_id} not found")
            return None

        case = (
            RecoveryCase.objects
            .filter(customer=customer, flag='c', status__in=['open', 'in_progress', 'reopened'])
            .order_by('-created_at')
            .first()
        )

        return {
            "customer_id": customer.id,
            "customer_name": customer.name or "Customer",
            "phone_number": customer.phone_number,
            "account_reference": customer.account_reference,
            "external_customer_id": customer.external_customer_id,
            "preferred_language": customer.preferred_language or "hi-IN",
            "do_not_call": customer.do_not_call,
            "do_not_call_reason": customer.do_not_call_reason,
            "open_case": self._serialize_case(case) if case else None,
        }

    def get_customer_by_phone(self, phone_number):
        """Lookup by phone for inbound call routing."""
        from recovery_agent.models import Customer
        try:
            customer = Customer.objects.get(phone_number=phone_number, flag='c')
            return self.get_recovery_profile(customer.id)
        except Customer.DoesNotExist:
            return None

    # ───────────────────────────────────────────────────────────
    # Payment / balance / due-date  (read-only — PaymentRecord is the
    # source of truth, never customer speech)
    # ───────────────────────────────────────────────────────────

    def get_outstanding_balance(self, customer_id):
        """Verified total outstanding across all open PaymentRecords."""
        from recovery_agent.models import PaymentRecord
        total = (
            PaymentRecord.objects
            .filter(
                customer_id=customer_id, flag='c',
                status__in=['pending', 'link_sent', 'partially_paid', 'failed'],
            )
            .aggregate(total=Sum('outstanding_amount'))
        )['total'] or Decimal('0')
        return {
            "customer_id": customer_id,
            "outstanding_amount": str(total),
            "currency": "INR",
            "verified": True,
            "as_of": timezone.now().isoformat(),
        }

    def get_payment_due_date(self, customer_id):
        """Earliest expiry/due date across pending payment records."""
        from recovery_agent.models import PaymentRecord
        earliest = (
            PaymentRecord.objects
            .filter(
                customer_id=customer_id, flag='c',
                status__in=['pending', 'link_sent', 'partially_paid', 'failed'],
            )
            .order_by('expires_at')
            .values('id', 'amount_due', 'outstanding_amount', 'expires_at', 'description')
            .first()
        )
        if not earliest:
            return {"customer_id": customer_id, "due_date": None, "verified": True}
        return {
            "customer_id": customer_id,
            "due_date": earliest["expires_at"].date().isoformat() if earliest["expires_at"] else None,
            "amount_due": str(earliest["amount_due"]),
            "outstanding_amount": str(earliest["outstanding_amount"]),
            "description": earliest["description"],
            "verified": True,
        }

    def get_payment_status(self, customer_id, payment_record_id=None):
        """Status of a specific payment (or the most recent one)."""
        from recovery_agent.models import PaymentRecord
        qs = PaymentRecord.objects.filter(customer_id=customer_id, flag='c')
        if payment_record_id:
            qs = qs.filter(id=payment_record_id)
        rec = qs.order_by('-created_at').first()
        if not rec:
            return {"customer_id": customer_id, "status": "no_payment_record", "verified": True}
        return {
            "customer_id": customer_id,
            "payment_record_id": rec.id,
            "amount_due": str(rec.amount_due),
            "amount_paid": str(rec.amount_paid),
            "outstanding_amount": str(rec.outstanding_amount),
            "status": rec.status,
            "provider": rec.provider,
            "provider_payment_id": rec.provider_payment_id,
            "short_url": rec.short_url,
            "paid_at": rec.paid_at.isoformat() if rec.paid_at else None,
            "verified": True,
        }

    # ───────────────────────────────────────────────────────────
    # Recovery case / event
    # ───────────────────────────────────────────────────────────

    def get_open_case(self, customer_id):
        from recovery_agent.models import RecoveryCase
        case = (
            RecoveryCase.objects
            .filter(customer_id=customer_id, flag='c', status__in=['open', 'in_progress', 'reopened'])
            .order_by('-created_at')
            .first()
        )
        return self._serialize_case(case) if case else None

    def update_case_status(self, case_id, status, outcome=None, current_intent=None,
                            promise_date=None, notes=None):
        from recovery_agent.models import RecoveryCase
        try:
            case = RecoveryCase.objects.get(id=case_id, flag='c')
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}

        case.status = status
        if outcome is not None:
            case.outcome = outcome
            case.current_outcome = outcome
        if current_intent is not None:
            case.current_intent = current_intent
        if promise_date is not None:
            case.promise_date = promise_date
        if status == 'closed':
            case.closed_at = timezone.now()
        case.last_contacted_at = timezone.now()
        case.save()

        if notes:
            # notes aren't a case field in the real model — record as an event instead
            self.record_recovery_event(case.id, event_type='note_added', notes=notes)

        return {"success": True, "case": self._serialize_case(case)}

    def record_recovery_event(self, case_id, event_type, intent=None, confidence=0.0,
                               payload=None, call_session_id=None, notes=""):
        from recovery_agent.models import RecoveryEvent, RecoveryCase, CallSession
        try:
            case = RecoveryCase.objects.get(id=case_id, flag='c')
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}

        call_session = None
        if call_session_id:
            call_session = CallSession.objects.filter(id=call_session_id).first()

        ev = RecoveryEvent.objects.create(
            case=case,
            event_type=event_type,
            intent=intent or "",
            confidence=confidence or 0.0,
            payload=payload or {},
            call_session=call_session,
            notes=notes,
        )
        return {"success": True, "event_id": ev.id}

    # ───────────────────────────────────────────────────────────
    # Exception outcomes
    # ───────────────────────────────────────────────────────────

    def mark_wrong_number(self, customer_id, notes=""):
        from recovery_agent.models import Customer
        try:
            c = Customer.objects.get(id=customer_id, flag='c')
        except Customer.DoesNotExist:
            return {"success": False}
        c.do_not_call = True
        c.do_not_call_reason = notes or "wrong_number"
        c.save(update_fields=["do_not_call", "do_not_call_reason", "updated_at"])
        return {"success": True}

    def mark_account_not_owned(self, customer_id, notes=""):
        return self.mark_wrong_number(customer_id, notes=notes or "account_not_owned")

    def mark_complaint(self, case_id, complaint_text):
        from recovery_agent.models import RecoveryCase, RecoveryEvent
        try:
            case = RecoveryCase.objects.get(id=case_id, flag='c')
        except RecoveryCase.DoesNotExist:
            return {"success": False, "error": "case not found"}
        case.status = 'in_progress'
        case.current_outcome = 'complaint'
        case.save(update_fields=["status", "current_outcome", "updated_at"])
        RecoveryEvent.objects.create(
            case=case,
            event_type='complaint_created',
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
            "customer_id": case.customer_id,
            "campaign_id": case.campaign_id,
            "case_type": case.case_type,
            "status": case.status,
            "priority": case.priority,
            "outcome": case.outcome,
            "current_intent": case.current_intent,
            "current_outcome": case.current_outcome,
            "amount_due": str(case.amount_due),
            "amount_recovered": str(case.amount_recovered),
            "currency": case.currency,
            "due_date": case.due_date.isoformat() if case.due_date else None,
            "promise_date": case.promise_date.isoformat() if case.promise_date else None,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "closed_at": case.closed_at.isoformat() if case.closed_at else None,
        }


crm_service = CRMService()