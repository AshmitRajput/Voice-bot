"""
RecoverAI — Domain Models

Single-tenant MVP. Exactly 13 models, matching the RecoverAI cleanup plan.
No Dealer / Branch / Vehicle / Segment / Campaign(old) / Appointment / CSV-import
models — those were Honda-dealership-specific and have been removed.

Every soft-deletable row carries `flag='c'` (current) / `flag='d'` (deleted) via
LogicalDeleteMixin. Hard deletes are never performed on business-history rows.

Model list:
    Customer, RecoveryCampaign, RecoveryCase, RecoveryEvent, Callback,
    PaymentRecord, PaymentEvent, CallSession, ConversationTurn,
    KnowledgeDocument, LLMSetting, TTSVoice, ServiceErrorLog
"""
import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone


# ═══════════════════════════════════════════════════════════════
# MIXINS
# ═══════════════════════════════════════════════════════════════

class LogicalDeleteMixin(models.Model):
    """Soft-delete column: flag='c' is current; flag='d' is deleted (never hard delete)."""
    flag = models.CharField(max_length=1, default='c', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ═══════════════════════════════════════════════════════════════
# CUSTOMER
# ═══════════════════════════════════════════════════════════════

class Customer(LogicalDeleteMixin):
    """The person/account being contacted for recovery."""
    phone_number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200, blank=True, default='')
    email = models.EmailField(blank=True, default='')

    # CRM linkage — this is how we tie the local row back to the source system.
    account_reference = models.CharField(max_length=100, blank=True, default='')
    external_customer_id = models.CharField(max_length=100, blank=True, default='')

    do_not_call = models.BooleanField(default=False)
    do_not_call_reason = models.CharField(max_length=200, blank=True, default='')
    preferred_language = models.CharField(max_length=10, blank=True, default='hi-IN')

    # Lightweight counter used by the admin list view / dashboard
    total_calls = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['do_not_call']),
            models.Index(fields=['account_reference']),
        ]

    def __str__(self):
        return f"{self.name or 'Customer'} ({self.phone_number})"


# ═══════════════════════════════════════════════════════════════
# RECOVERY CAMPAIGN — outbound push
# ═══════════════════════════════════════════════════════════════

class RecoveryCampaign(LogicalDeleteMixin):
    """
    A planned outbound recovery push. A campaign contains one or more
    RecoveryCase rows (one per targeted customer).
    """
    name = models.CharField(max_length=200)
    campaign_type = models.CharField(max_length=50, default='payment')
    description = models.TextField(blank=True, default='')

    status = models.CharField(
        max_length=20,
        choices=[
            ('draft', 'Draft'),
            ('scheduled', 'Scheduled'),
            ('running', 'Running'),
            ('paused', 'Paused'),
            ('completed', 'Completed'),
            ('archived', 'Archived'),
        ],
        default='draft',
    )

    target_due_within_days = models.PositiveIntegerField(default=14)

    # Stats counters (denormalised — cheap to display)
    customer_count = models.PositiveIntegerField(default=0)
    calls_attempted = models.PositiveIntegerField(default=0)
    calls_connected = models.PositiveIntegerField(default=0)
    cases_recovered = models.PositiveIntegerField(default=0)
    amount_recovered = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.status})"


# ═══════════════════════════════════════════════════════════════
# RECOVERY CASE — one per (customer × campaign)
# ═══════════════════════════════════════════════════════════════

class RecoveryCase(LogicalDeleteMixin):
    """
    The central business record: one row = one customer's one recovery
    obligation. Lifecycle: open -> in_progress -> (closed | reopened).
    """
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='recovery_cases')
    campaign = models.ForeignKey(
        RecoveryCampaign, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cases',
    )

    case_type = models.CharField(max_length=50, default='payment')

    status = models.CharField(
        max_length=20,
        choices=[
            ('open', 'Open'),
            ('in_progress', 'In progress'),
            ('closed', 'Closed'),
            ('reopened', 'Reopened'),
        ],
        default='open',
    )
    priority = models.CharField(
        max_length=10,
        choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
        default='medium',
    )

    # Final outcome (only meaningful when status='closed')
    outcome = models.CharField(
        max_length=40, blank=True, default='',
        help_text='recovered / declined / callback_scheduled / complaint / wrong_number / '
                   'account_not_owned / do_not_call / unreachable',
    )

    amount_due = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    amount_recovered = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    currency = models.CharField(max_length=10, default='INR')
    due_date = models.DateField(null=True, blank=True)

    # Live tracking fields the LLM/tools read+write via RecoveryService
    current_intent = models.CharField(max_length=40, blank=True, default='')
    current_outcome = models.CharField(max_length=40, blank=True, default='')
    promise_date = models.DateField(null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    next_followup_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of CRM truth at case-open, so the case stays immutable even if
    # customer fields change later.
    snapshot = models.JSONField(default=dict, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    last_call = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['due_date']),
        ]

    def __str__(self):
        return f"Case#{self.id} {self.customer.phone_number} [{self.status}/{self.outcome}]"


class RecoveryEvent(LogicalDeleteMixin):
    """
    Append-only audit log of everything that happens to a RecoveryCase:
    attempts, intent changes, promises, payments, callbacks, escalations.
    """
    case = models.ForeignKey(RecoveryCase, on_delete=models.CASCADE, related_name='events')

    event_type = models.CharField(
        max_length=50,
        help_text='call_attempted / call_connected / payment_status_checked / '
                   'payment_link_sent / payment_verified / promise_recorded / '
                   'payment_refused / dispute_created / complaint_created / '
                   'callback_requested / callback_scheduled / callback_completed / '
                   'wrong_number / account_not_owned / human_escalation / case_closed',
    )
    intent = models.CharField(max_length=40, blank=True, default='')
    confidence = models.FloatField(default=0.0)

    call_session = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    payload = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['case', 'event_type']),
            models.Index(fields=['occurred_at']),
        ]

    def __str__(self):
        return f"{self.event_type} @ {self.occurred_at:%Y-%m-%d %H:%M} (case={self.case_id})"


# ═══════════════════════════════════════════════════════════════
# CALLBACK SCHEDULING
# ═══════════════════════════════════════════════════════════════

class Callback(LogicalDeleteMixin):
    """
    A scheduled future contact. Created only when the customer explicitly
    requests one (e.g. 'call me this evening'), or when policy requires a
    follow-up (e.g. after a complaint).
    """
    STATUS_CHOICES = [
        ('requested', 'Requested'),
        ('scheduled', 'Scheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('missed', 'Missed'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='callbacks')
    recovery_case = models.ForeignKey(
        RecoveryCase, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='callbacks',
    )
    session = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
        help_text='The originating call session that produced this callback, if any.',
    )

    scheduled_for = models.DateTimeField(db_index=True)
    reason = models.CharField(max_length=200, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested')

    # Optional free-text hint parsed from the customer's words (e.g. "shaam ko")
    requested_window = models.CharField(max_length=100, blank=True, default='')

    attempted_at = models.DateTimeField(null=True, blank=True)
    completed_session = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['scheduled_for']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['customer', 'status']),
        ]

    def __str__(self):
        return f"Callback {self.customer.phone_number} @ {self.scheduled_for:%Y-%m-%d %H:%M} [{self.status}]"


# ═══════════════════════════════════════════════════════════════
# PAYMENT RECOVERY
# ═══════════════════════════════════════════════════════════════

class PaymentRecord(LogicalDeleteMixin):
    """
    The authoritative current payment state for a RecoveryCase. The AI never
    writes to this table directly based on customer speech — only
    PaymentService, driven by the actual payment provider/CRM, may update it.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('link_sent', 'Link sent'),
        ('partially_paid', 'Partially paid'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
        ('disputed', 'Disputed'),
    ]
    PROVIDER_CHOICES = [
        ('razorpay', 'Razorpay'),
        ('stripe', 'Stripe'),
        ('manual', 'Manual'),
        ('cash', 'Cash'),
        ('upi', 'UPI'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='payments')
    recovery_case = models.ForeignKey(
        RecoveryCase, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='payments',
    )
    session = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    outstanding_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    currency = models.CharField(max_length=10, default='INR')
    description = models.CharField(max_length=300, blank=True, default='')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default='razorpay')

    provider_payment_id = models.CharField(max_length=100, blank=True, default='')
    provider_order_id = models.CharField(max_length=100, blank=True, default='')
    provider_link_id = models.CharField(max_length=100, blank=True, default='')
    short_url = models.URLField(blank=True, default='')

    paid_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['provider_payment_id']),
        ]

    def __str__(self):
        return f"Payment {self.customer.phone_number} {self.amount_due} {self.currency} [{self.status}]"


class PaymentEvent(LogicalDeleteMixin):
    """Append-only audit of payment-state transitions (webhooks, verifications)."""
    payment = models.ForeignKey(PaymentRecord, on_delete=models.CASCADE, related_name='events')

    event_type = models.CharField(
        max_length=50,
        help_text='payment_link_created / payment_link_sent / payment_initiated / '
                  'payment_success / payment_failed / payment_verification_requested / '
                  'payment_refunded / webhook_received',
    )
    provider_event_id = models.CharField(max_length=150, blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['payment', 'occurred_at']),
            models.Index(fields=['event_type']),
        ]

    def __str__(self):
        return f"{self.event_type} ({self.payment_id}) @ {self.occurred_at:%Y-%m-%d %H:%M}"


# ═══════════════════════════════════════════════════════════════
# CALL SESSION & TURNS
# ═══════════════════════════════════════════════════════════════

class CallSession(LogicalDeleteMixin):
    """A single phone call (outbound or inbound)."""
    session_id = models.CharField(max_length=64, unique=True)
    dialer_call_id = models.CharField(max_length=100, blank=True, default='')

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='calls')
    recovery_case = models.ForeignKey(
        RecoveryCase, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='calls',
    )
    campaign = models.ForeignKey(
        RecoveryCampaign, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    agent = models.ForeignKey(
        'LLMSetting', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ('queued', 'Queued'),
            ('ringing', 'Ringing'),
            ('ongoing', 'Ongoing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('busy', 'Busy'),
            ('no_answer', 'No answer'),
            ('dropped', 'Dropped'),
        ],
        default='ongoing',
    )
    direction = models.CharField(
        max_length=10,
        choices=[('outbound', 'Outbound'), ('inbound', 'Inbound')],
        default='outbound',
    )

    started_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    dialer_billed_seconds = models.PositiveIntegerField(default=0)

    transcript = models.JSONField(default=list, blank=True)
    intent_history = models.JSONField(default=list, blank=True)
    final_intent_code = models.CharField(max_length=40, blank=True, default='')

    intent = models.CharField(max_length=40, blank=True, default='')
    recovery_outcome = models.CharField(max_length=40, blank=True, default='')
    recovery_notes = models.TextField(blank=True, default='')
    call_summary = models.TextField(blank=True, default='')

    accuracy = models.FloatField(null=True, blank=True)
    filler_accuracy = models.FloatField(null=True, blank=True)
    llm_accuracy = models.FloatField(null=True, blank=True)

    recording_stereo = models.CharField(max_length=500, blank=True, default='')
    recording_mixed = models.CharField(max_length=500, blank=True, default='')

    stt_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    tts_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    llm_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    dialer_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    total_cost = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))

    stt_latency_ms = models.PositiveIntegerField(default=0)
    llm_latency_ms = models.PositiveIntegerField(default=0)
    tts_latency_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'started_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Call {self.session_id} [{self.status}]"


class ConversationTurn(LogicalDeleteMixin):
    """One utterance in a CallSession — either customer or bot."""
    SPEAKER_CHOICES = [
        ('customer', 'Customer'),
        ('bot', 'Bot'),
        ('system', 'System'),
    ]

    call_session = models.ForeignKey(CallSession, on_delete=models.CASCADE, related_name='turns')

    speaker = models.CharField(max_length=10, choices=SPEAKER_CHOICES)
    text = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    intent = models.CharField(max_length=40, blank=True, default='')
    confidence = models.FloatField(default=0.0)
    entities = models.JSONField(default=dict, blank=True)

    filler_text = models.CharField(max_length=200, blank=True, default='')
    timing = models.JSONField(default=dict, blank=True)

    accuracy = models.FloatField(null=True, blank=True)
    filler_accuracy = models.FloatField(null=True, blank=True)
    llm_accuracy = models.FloatField(null=True, blank=True)

    stt_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    tts_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    llm_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['call_session', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.speaker} @ {self.timestamp:%H:%M:%S} ({self.call_session.session_id})"


# ═══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE (RAG)
# ═══════════════════════════════════════════════════════════════

class KnowledgeDocument(LogicalDeleteMixin):
    """
    A document indexed into the RAG vector store. Django is the source of
    truth for document metadata; Chroma is the vector index (see rag_service.py).
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('indexed', 'Indexed'),
        ('stale', 'Stale'),
        ('failed', 'Failed'),
    ]

    doc_id = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=300)
    category = models.CharField(
        max_length=100, blank=True, default='',
        help_text='payment_policy / payment_methods / payment_link / late_payment / '
                  'promise_to_pay / hardship / dispute / complaint / callback / '
                  'escalation / communication_policy',
    )

    content = models.TextField()
    source = models.CharField(max_length=300, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    chunk_count = models.PositiveIntegerField(default=0)
    collection_name = models.CharField(max_length=200, blank=True, default='')
    indexed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['category']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.doc_id} [{self.category}/{self.status}]"


# ═══════════════════════════════════════════════════════════════
# AI CONFIGURATION
# ═══════════════════════════════════════════════════════════════

class TTSVoice(LogicalDeleteMixin):
    """A single TTS voice (e.g. hi-IN-Wavenet-A) provided by a specific vendor."""
    GENDER_CHOICES = [('male', 'Male'), ('female', 'Female'), ('neutral', 'Neutral')]

    voice_name = models.CharField(max_length=200)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    provider_voice_id = models.CharField(max_length=100, blank=True, default='')
    provider_name = models.CharField(max_length=100, default='Murf')
    language = models.CharField(max_length=10, default='hi-IN')
    is_active = models.BooleanField(default=True)
    sample_url = models.URLField(blank=True, default='')

    class Meta:
        ordering = ['voice_name']
        unique_together = [('voice_name', 'provider_name')]

    def __str__(self):
        return f"{self.voice_name} ({self.provider_name})"


class LLMSetting(LogicalDeleteMixin):
    """
    Agent / persona / LLM configuration. Single-tenant MVP: there is normally
    just one active row, but the model allows several (e.g. for A/B testing)
    with `is_active` marking which one is currently live.
    """
    name = models.CharField(max_length=100, default='default')
    is_active = models.BooleanField(default=True)

    provider = models.CharField(max_length=50, default='gemini')
    model = models.CharField(max_length=100, default='gemini-2.5-flash-lite')
    temperature = models.FloatField(default=0.4)
    max_tokens = models.PositiveIntegerField(default=1000)

    persona_name = models.CharField(max_length=100, default='RecoverAI')
    opening_line = models.TextField(blank=True, default='')
    system_prompt = models.TextField()
    behaviour = models.TextField(blank=True, default='')

    voice = models.ForeignKey(TTSVoice, on_delete=models.PROTECT, related_name='+')

    tone = models.PositiveSmallIntegerField(default=72)
    pace = models.PositiveSmallIntegerField(default=50)
    barge_in_threshold = models.PositiveSmallIntegerField(default=65)
    max_turns = models.PositiveSmallIntegerField(default=10)
    allow_customer_barge_in = models.BooleanField(default=True)

    language = models.CharField(max_length=10, default='hi-IN')
    response_max_chars = models.PositiveIntegerField(default=240)
    questions_per_turn_max = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['-is_active', 'name']

    def __str__(self):
        return f"{self.persona_name} [{self.name}]"


# ═══════════════════════════════════════════════════════════════
# OPERATIONS / OBSERVABILITY
# ═══════════════════════════════════════════════════════════════

class ServiceErrorLog(models.Model):
    """Errors from external providers (STT, TTS, LLM, dialer, payment, RAG)."""
    SEVERITY_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]

    call_session = models.ForeignKey(
        CallSession, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    session_id = models.CharField(max_length=64, blank=True, default='')

    provider = models.CharField(
        max_length=30,
        help_text='stt / tts / llm / dialer / payment / rag / other',
    )
    stage = models.CharField(max_length=60, blank=True, default='')
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='error')
    error_type = models.CharField(max_length=80, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    context = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['provider', 'created_at']),
            models.Index(fields=['severity', 'created_at']),
        ]

    def __str__(self):
        return f"{self.provider}/{self.stage} [{self.severity}] @ {self.created_at:%Y-%m-%d %H:%M}"