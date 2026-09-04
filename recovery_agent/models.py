"""
RecoverAI — Domain Models

Models are split into logical groups. Every soft-deletable row carries `flag='c'` (current)
to support logical deletes — a hard delete is never done on customer/vehicle/call data.

Naming follows existing patterns where they made sense (Dealer, Branch, Customer,
Vehicle, CallSession, ConversationTurn, Segment, TTSVoice, LLMSetting,
KnowledgeDocument, ServiceErrorLog). Recovery-specific entities are named explicitly:
RecoveryCase, RecoveryEvent, Callback, PaymentRecord, PaymentEvent, RecoveryCampaign. """
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
# TENANT: DEALER & BRANCH
# ═══════════════════════════════════════════════════════════════

class Dealer(LogicalDeleteMixin):
    """A dealer / tenant. All customer & call data is scoped to a Dealer."""
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True)
    is_active = models.BooleanField(default=True)
    default_branch = models.ForeignKey(
        'Branch',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )

    # Recovery / runtime defaults (legacy — kept for back-compat with old views)
    active_prompt = models.TextField(blank=True, default='')
    tts_provider = models.CharField(max_length=50, blank=True, default='google')
    tts_voice = models.CharField(max_length=100, blank=True, default='hi-IN-Wavenet-A')

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Branch(LogicalDeleteMixin):
    """A physical branch / location belonging to a Dealer."""
    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name='branches')
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    phone = models.CharField(max_length=20, blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        unique_together = [('dealer', 'name')]

    def __str__(self):
        return f"{self.dealer.name} / {self.name}"


# ═══════════════════════════════════════════════════════════════
# CUSTOMER & VEHICLE
# ═══════════════════════════════════════════════════════════════

class Customer(LogicalDeleteMixin):
    """A customer that the recovery agent will (or has) contacted."""
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='customers')
    phone_number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    default_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    do_not_call = models.BooleanField(default=False)
    do_not_call_reason = models.CharField(max_length=200, blank=True, default='')
    preferred_language = models.CharField(max_length=10, blank=True, default='hi-IN')

    # Lightweight counter used by the admin list view
    total_calls = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['dealer', 'do_not_call']),
        ]

    def __str__(self):
        return f"{self.name or 'Customer'} ({self.phone_number})"


class Vehicle(LogicalDeleteMixin):
    """
    A vehicle owned by a Customer. Each Vehicle carries its own recovery context
    (next_service_due_date, insurance_expiry_date, amc_expiry_date) so the
    recovery agent knows exactly WHY it's calling. """
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='vehicles')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='vehicles')

    vehicle_name = models.CharField(max_length=100, blank=True, default='')
    vehicle_model = models.CharField(max_length=100, blank=True, default='')
    registration_no = models.CharField(max_length=50, blank=True, default='')
    chassis_no = models.CharField(max_length=50, blank=True, default='')
    color = models.CharField(max_length=50, blank=True, default='')
    is_sold_off = models.BooleanField(default=False)

    # Service context (drives why-we-call)
    last_service_type = models.CharField(max_length=30, blank=True, default='none')
    last_service_date = models.DateField(null=True, blank=True)
    next_service_type = models.CharField(max_length=30, blank=True, default='')
    next_service_due_date = models.DateField(null=True, blank=True)

    insurance_expiry_date = models.DateField(null=True, blank=True)
    insurance_provider = models.CharField(max_length=100, blank=True, default='')
    amc_expiry_date = models.DateField(null=True, blank=True)

    purchased_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    last_service_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    # Reminder opt-out per row (vs Customer.do_not_call which is account-wide)
    on_reminder = models.BooleanField(default=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['customer', 'is_sold_off']),
            models.Index(fields=['next_service_due_date']),
        ]

    def __str__(self):
        return f"{self.vehicle_model or self.vehicle_name or 'Vehicle'} ({self.registration_no})"


# ═══════════════════════════════════════════════════════════════
# CAMPAIGN — outbound call queue
# ═══════════════════════════════════════════════════════════════

class RecoveryCampaign(LogicalDeleteMixin):
    """
    A planned outbound recovery push. A campaign contains one or more
    RecoveryCase rows (one per targeted customer). """
    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name='campaigns')
    name = models.CharField(max_length=200)
    module = models.CharField(max_length=50, default='service_reminder')
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

    # Targeting rules (simple JSON for now — extensible later)
    target_module = models.CharField(max_length=50, default='service_reminder')
    target_due_within_days = models.PositiveIntegerField(default=14)

    # Stats counters (denormalised — cheap to display)
    customer_count = models.PositiveIntegerField(default=0)
    calls_attempted = models.PositiveIntegerField(default=0)
    calls_connected = models.PositiveIntegerField(default=0)
    cases_recovered = models.PositiveIntegerField(default=0)
    amount_recovered = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
    )

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
    The unit of recovery. One row = one customer's one recovery attempt.
    Lifecycle: open -> in_progress -> (closed | reopened)
    Outcome: recovered | declined | callback_scheduled | complaint |
             wrong_number | vehicle_sold | do_not_call | unreachable. """
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='recovery_cases')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='recovery_cases')
    campaign = models.ForeignKey(
        RecoveryCampaign,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cases',
    )

    module = models.CharField(
        max_length=50, default='service_reminder',
        help_text='Why we are calling — service_reminder / payment_pending / amc_due / etc.',
    )

    # Lifecycle
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

    # Final outcome (only meaningful when status='closed')
    outcome = models.CharField(
        max_length=40, default='',
        help_text='recovered / declined / callback_scheduled / complaint / wrong_number / vehicle_sold / unreachable',
    )

    # Recovery monetary fields (only relevant for payment / amc modules)
    amount_due = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    amount_recovered = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    # Snapshots of CRM truth at case-open (so the case is immutable even if
    # customer/vehicle fields change later)
    snapshot = models.JSONField(default=dict, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)
    last_call = models.ForeignKey(
        'CallSession',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['dealer', 'status']),
            models.Index(fields=['customer', 'status']),
        ]

    def __str__(self):
        return f"Case#{self.id} {self.customer.phone_number} [{self.status}/{self.outcome}]"


class RecoveryEvent(LogicalDeleteMixin):
    """
    Append-only audit log of significant things that happened to a RecoveryCase:
    attempts, status changes, complaints, payments, callbacks, etc.
    Used by the recovery analytics dashboard. """
    case = models.ForeignKey(RecoveryCase, on_delete=models.CASCADE, related_name='events')
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='recovery_events')

    event_type = models.CharField(
        max_length=50,
        help_text='call_attempted / call_connected / customer_accepted / customer_declined / '
                  'payment_link_sent / payment_done / callback_scheduled / complaint_opened / '
                  'case_closed / case_reopened / wrong_number / vehicle_sold',
    )
    intent = models.CharField(max_length=40, blank=True, default='')
    confidence = models.FloatField(default=0.0)

    call_session = models.ForeignKey(
        'CallSession', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    # Structured event-specific payload
    payload = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['case', 'event_type']),
            models.Index(fields=['dealer', 'occurred_at']),
        ]

    def __str__(self):
        return f"{self.event_type} @ {self.occurred_at:%Y-%m-%d %H:%M} (case={self.case_id})"


# ═══════════════════════════════════════════════════════════════
# CALL SESSION & TURNS
# ═══════════════════════════════════════════════════════════════

class CallSession(LogicalDeleteMixin):
    """A single phone call (outbound or inbound)."""
    session_id = models.CharField(max_length=64, unique=True)
    dialer_call_id = models.CharField(max_length=100, blank=True, default='')

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='calls')
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='calls')
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    recovery_case = models.ForeignKey(
        RecoveryCase,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='calls',
    )

    segment = models.ForeignKey(
        'Segment',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    agent = models.ForeignKey(
        'LLMSetting',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )

    # Legacy relationships (kept optional for the existing consumer patterns)
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    # Call lifecycle
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

    # Timing
    started_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    dialer_billed_seconds = models.PositiveIntegerField(default=0)

    # Conversation state
    transcript = models.JSONField(default=list, blank=True)
    intent_history = models.JSONField(default=list, blank=True)
    final_intent_code = models.CharField(max_length=40, blank=True, default='')

    # Outcome
    intent = models.CharField(max_length=40, blank=True, default='')
    recovery_outcome = models.CharField(max_length=40, blank=True, default='')
    recovery_notes = models.TextField(blank=True, default='')

    call_summary = models.TextField(blank=True, default='')
    accuracy = models.FloatField(null=True)
    filler_accuracy = models.FloatField(null=True)
    llm_accuracy = models.FloatField(null=True)

    # Recordings (filesystem paths, NOT FileFields)
    recording_stereo = models.CharField(max_length=500, blank=True, default='')
    recording_mixed = models.CharField(max_length=500, blank=True, default='')

    # Cost tracking
    stt_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    tts_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    llm_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    dialer_pricing = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))
    total_cost = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal('0'))

    # Latency tracking
    stt_latency_ms = models.PositiveIntegerField(default=0)
    llm_latency_ms = models.PositiveIntegerField(default=0)
    tts_latency_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['dealer', 'started_at']),
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
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='+')

    speaker = models.CharField(max_length=10, choices=SPEAKER_CHOICES)
    text = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    # Intent classification result (only set for customer turns)
    intent = models.CharField(max_length=40, blank=True, default='')
    confidence = models.FloatField(default=0.0)

    # Tool / filler text the LLM decided to insert
    filler_text = models.CharField(max_length=200, blank=True, default='')

    # Latency breakdown for this turn
    timing = models.JSONField(default=dict, blank=True)

    # Per-turn accuracy scores (post-call)
    accuracy = models.FloatField(null=True)
    filler_accuracy = models.FloatField(null=True)
    llm_accuracy = models.FloatField(null=True)

    # Cost per turn
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


class CorrectIntent(LogicalDeleteMixin):
    """
    Post-call QA rows. When the call summary model decides a turn was misclassified
    in real time, this row records (turn, suggested_intent) so the dataset can
    be used to retrain / evaluate the classifier. """
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='+')
    conversation = models.ForeignKey(CallSession, on_delete=models.CASCADE, related_name='+')
    turn = models.ForeignKey(ConversationTurn, on_delete=models.CASCADE, related_name='corrections')
    customer_text = models.TextField(blank=True, default='')
    intent = models.CharField(max_length=40, blank=True, default='')
    suggested_intent = models.CharField(max_length=40, blank=True, default='')
    filler = models.CharField(max_length=200, blank=True, default='')
    suggested_filler = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Correction turn={self.turn_id} {self.intent}→{self.suggested_intent}"


# ═══════════════════════════════════════════════════════════════
# CALLBACK SCHEDULING
# ═══════════════════════════════════════════════════════════════

class Callback(LogicalDeleteMixin):
    """
    A scheduled outbound call at a future date/time. Created when the
    customer says 'call me later' OR when the AI decides to follow up
    (e.g. after a complaint is opened). """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
    ]

    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='callbacks')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='callbacks')
    session = models.ForeignKey(
        CallSession, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
        help_text='The originating call session that produced this callback, if any.',
    )
    recovery_case = models.ForeignKey(
        RecoveryCase, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='callbacks',
    )

    scheduled_for = models.DateTimeField(db_index=True)
    reason = models.CharField(max_length=200, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Optional time-window hint parsed from the customer's words
    requested_window = models.CharField(max_length=100, blank=True, default='')

    # Audit
    attempted_at = models.DateTimeField(null=True, blank=True)
    completed_session = models.ForeignKey(
        CallSession, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['scheduled_for']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['dealer', 'status']),
        ]

    def __str__(self):
        return f"Callback {self.customer.phone_number} @ {self.scheduled_for:%Y-%m-%d %H:%M} [{self.status}]"


# ═══════════════════════════════════════════════════════════════
# PAYMENT RECOVERY
# ═══════════════════════════════════════════════════════════════

class PaymentRecord(LogicalDeleteMixin):
    """
    A payment that is owed / has been attempted / has succeeded. The source
    of truth is always the payment provider — this table caches the latest
    state for fast lookup during calls. The AI MUST NOT decide payment truth;
    it only triggers create_payment_link() and reports payment_done signals. """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('link_sent', 'Link sent'),
        ('in_progress', 'Customer paying'),
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

    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='payments')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='payments')
    recovery_case = models.ForeignKey(
        RecoveryCase, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='payments',
    )
    session = models.ForeignKey(
        CallSession, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default='INR')
    description = models.CharField(max_length=300, blank=True, default='')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default='razorpay')

    # Provider-side identifiers
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
            models.Index(fields=['dealer', 'status']),
            models.Index(fields=['provider_payment_id']),
        ]

    def __str__(self):
        return f"Payment {self.customer.phone_number} {self.amount} {self.currency} [{self.status}]"


class PaymentEvent(LogicalDeleteMixin):
    """Append-only audit of payment-state transitions (used by analytics + dispute triage)."""
    payment = models.ForeignKey(PaymentRecord, on_delete=models.CASCADE, related_name='events')
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='+')

    event_type = models.CharField(
        max_length=50,
        help_text='link_created / link_sent / payment_attempted / payment_succeeded / '
                  'payment_failed / refund_issued / disputed / webhook_received',
    )
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['payment', 'occurred_at']),
            models.Index(fields=['dealer', 'event_type']),
        ]

    def __str__(self):
        return f"{self.event_type} ({self.payment_id}) @ {self.occurred_at:%Y-%m-%d %H:%M}"


# ═══════════════════════════════════════════════════════════════
# AI PERSONA: TTS VOICE / SEGMENT / LLM SETTING
# ═══════════════════════════════════════════════════════════════

class TTSVoice(LogicalDeleteMixin):
    """A single TTS voice (e.g. hi-IN-Wavenet-A) provided by a specific vendor."""
    GENDER_CHOICES = [('male', 'Male'), ('female', 'Female'), ('neutral', 'Neutral')]

    voice_name = models.CharField(max_length=200)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    provider_id = models.PositiveIntegerField(default=1)
    provider_name = models.CharField(max_length=100, default='Murf')
    is_active = models.BooleanField(default=True)
    sample_url = models.URLField(blank=True, default='')

    class Meta:
        ordering = ['voice_name']
        unique_together = [('voice_name', 'provider_name')]

    def __str__(self):
        return f"{self.voice_name} ({self.provider_name})"


class Segment(LogicalDeleteMixin):
    """
    A logical group of LLM settings — e.g. 'service_reminder', 'payment_pending'.
    Recovery campaigns are targeted by segment. """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    module = models.CharField(
        max_length=50, blank=True, default='',
        help_text='Logical module key — service_reminder / payment_pending / amc_due / etc.',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class LLMSetting(LogicalDeleteMixin):
    """AI persona config for a given Segment — what the agent says, how it sounds, how it behaves."""
    segment = models.OneToOneField(Segment, on_delete=models.CASCADE, related_name='llm_setting')
    dealer = models.ForeignKey(
        Dealer, on_delete=models.PROTECT,
        null=True, blank=True, related_name='+',
    )
    module = models.CharField(max_length=50, default='service_reminder')

    persona_name = models.CharField(max_length=100)
    agent_name = models.CharField(max_length=100, blank=True, default='')
    opening_line = models.TextField()
    system_prompt = models.TextField()
    behaviour = models.TextField(blank=True, default='')

    voice = models.ForeignKey(TTSVoice, on_delete=models.PROTECT, related_name='+')

    # Conversation tuning
    tone = models.PositiveSmallIntegerField(default=72)
    pace = models.PositiveSmallIntegerField(default=50)
    barge_in_threshold = models.PositiveSmallIntegerField(default=65)
    max_turns = models.PositiveSmallIntegerField(default=10)
    allow_customer_barge_in = models.BooleanField(default=True)

    # Language / locale
    language = models.CharField(max_length=10, default='hi-IN')
    response_max_chars = models.PositiveIntegerField(default=240)
    questions_per_turn_max = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ['segment__name']

    def __str__(self):
        return f"{self.persona_name} [{self.segment.name}]"


# ═══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE
# ═══════════════════════════════════════════════════════════════

class KnowledgeDocument(LogicalDeleteMixin):
    """
    A document indexed into the RAG vector store. Each document is scoped to
    a Dealer, optionally to a list of branches, and to a module
    (service / warranty / payment_faq / escalation / etc.). """
    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name='knowledge_documents')
    doc_id = models.CharField(max_length=100)

    title = models.CharField(max_length=300)
    category = models.CharField(max_length=100, blank=True, default='')
    module = models.CharField(max_length=50, default='service')

    branch_ids = models.JSONField(
        default=list, blank=True,
        help_text='List of branch.id values this doc applies to. [] = applies to all branches.',
    )

    content = models.TextField()
    source = models.CharField(max_length=300, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    # Indexing status
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('indexed', 'Indexed'),
        ('stale', 'Stale'),
        ('failed', 'Failed'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    chunk_count = models.PositiveIntegerField(default=0)
    collection_name = models.CharField(max_length=200, blank=True, default='')
    indexed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        unique_together = [('dealer', 'doc_id')]
        indexes = [
            models.Index(fields=['dealer', 'module']),
        ]

    def __str__(self):
        return f"{self.doc_id} [{self.module}/{self.status}]"


# ═══════════════════════════════════════════════════════════════
# OPERATIONS / OBSERVABILITY
# ═══════════════════════════════════════════════════════════════

class ServiceErrorLog(models.Model):
    """Errors from external providers (STT, TTS, LLM, dialer, payment)."""
    SEVERITY_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ]

    dealer = models.ForeignKey(
        Dealer, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
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


# ═══════════════════════════════════════════════════════════════
# LEGACY — kept only if your migrations still reference them
# (safe to remove once you've squashed old migrations).
# ═══════════════════════════════════════════════════════════════

class ServiceSchedule(LogicalDeleteMixin):
    """
    Service-due rule: when a vehicle finishes service of type X, schedule
    the next service of type Y after N days. Used by the dashboard to
    auto-compute next_service_due_date when admin only enters last_service info. """
    dealer = models.ForeignKey(Dealer, on_delete=models.PROTECT, related_name='+')
    vehicle_model = models.CharField(max_length=100, blank=True, default='')
    from_service = models.CharField(max_length=30, blank=True, default='')
    to_service = models.CharField(max_length=30, blank=True, default='')
    days_after = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['vehicle_model', 'from_service']

    def __str__(self):
        return f"{self.from_service} -> {self.to_service} (+{self.days_after}d)"


SERVICE_TYPE_CHOICES = [
    ('none', 'None'),
    ('1st_free', '1st free service'),
    ('2nd_free', '2nd free service'),
    ('3rd_free', '3rd free service'),
    ('paid', 'Paid service'),
    ('amc', 'AMC service'),
]