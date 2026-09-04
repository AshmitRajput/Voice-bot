"""RecoverAI admin — registers exactly the 13 RecoverAI models. No Honda/dealer leftovers."""
from django.contrib import admin
from .models import (
    Customer,
    RecoveryCampaign, RecoveryCase, RecoveryEvent, Callback,
    PaymentRecord, PaymentEvent,
    CallSession, ConversationTurn,
    KnowledgeDocument,
    LLMSetting, TTSVoice,
    ServiceErrorLog,
)


# ═══════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['phone_number', 'name', 'account_reference', 'do_not_call',
                     'preferred_language', 'total_calls', 'created_at']
    list_filter = ['do_not_call', 'preferred_language']
    search_fields = ['phone_number', 'name', 'email', 'account_reference', 'external_customer_id']
    readonly_fields = ['total_calls', 'created_at', 'updated_at']


# ═══════════════════════════════════════════════════════════════
# RECOVERY
# ═══════════════════════════════════════════════════════════════

class RecoveryEventInline(admin.TabularInline):
    model = RecoveryEvent
    extra = 0
    fields = ['event_type', 'intent', 'confidence', 'occurred_at']
    readonly_fields = ['occurred_at']
    ordering = ['-occurred_at']


class PaymentRecordInline(admin.TabularInline):
    model = PaymentRecord
    extra = 0
    fields = ['amount_due', 'amount_paid', 'outstanding_amount', 'currency',
              'status', 'provider', 'short_url', 'paid_at']
    readonly_fields = ['paid_at']


@admin.register(RecoveryCampaign)
class RecoveryCampaignAdmin(admin.ModelAdmin):
    list_display = ['name', 'campaign_type', 'status', 'customer_count',
                     'calls_attempted', 'cases_recovered', 'amount_recovered',
                     'started_at', 'finished_at']
    list_filter = ['status', 'campaign_type']
    search_fields = ['name', 'description']
    readonly_fields = ['customer_count', 'calls_attempted', 'calls_connected',
                       'cases_recovered', 'amount_recovered', 'started_at',
                       'finished_at', 'created_at', 'updated_at']


@admin.register(RecoveryCase)
class RecoveryCaseAdmin(admin.ModelAdmin):
    list_display = ['id', 'customer', 'case_type', 'amount_due', 'amount_recovered',
                     'status', 'priority', 'outcome', 'due_date', 'created_at']
    list_filter = ['status', 'priority', 'outcome', 'case_type']
    search_fields = ['customer__phone_number', 'customer__name']
    readonly_fields = ['closed_at', 'last_call', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    inlines = [RecoveryEventInline, PaymentRecordInline]


@admin.register(RecoveryEvent)
class RecoveryEventAdmin(admin.ModelAdmin):
    list_display = ['case', 'event_type', 'intent', 'confidence', 'occurred_at']
    list_filter = ['event_type']
    search_fields = ['case__id', 'intent', 'event_type']
    readonly_fields = ['occurred_at', 'created_at', 'updated_at']
    date_hierarchy = 'occurred_at'


@admin.register(Callback)
class CallbackAdmin(admin.ModelAdmin):
    list_display = ['id', 'customer', 'scheduled_for', 'status', 'reason', 'created_at']
    list_filter = ['status', 'reason']
    search_fields = ['customer__phone_number', 'reason', 'notes']
    readonly_fields = ['attempted_at', 'completed_session', 'created_at', 'updated_at']
    date_hierarchy = 'scheduled_for'


# ═══════════════════════════════════════════════════════════════
# PAYMENTS
# ═══════════════════════════════════════════════════════════════

@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = ['id', 'customer', 'amount_due', 'amount_paid', 'outstanding_amount',
                     'currency', 'status', 'provider', 'short_url', 'paid_at', 'created_at']
    list_filter = ['status', 'provider']
    search_fields = ['customer__phone_number', 'provider_payment_id',
                     'provider_order_id', 'description']
    readonly_fields = ['provider_payment_id', 'provider_order_id', 'provider_link_id',
                       'paid_at', 'expires_at', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'


@admin.register(PaymentEvent)
class PaymentEventAdmin(admin.ModelAdmin):
    list_display = ['payment', 'event_type', 'amount', 'occurred_at']
    list_filter = ['event_type']
    search_fields = ['payment__id', 'event_type', 'provider_event_id']
    readonly_fields = ['occurred_at', 'created_at', 'updated_at']
    date_hierarchy = 'occurred_at'


# ═══════════════════════════════════════════════════════════════
# CALLS
# ═══════════════════════════════════════════════════════════════

@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'customer', 'status', 'direction',
                     'final_intent_code', 'duration_seconds', 'total_cost', 'started_at']
    list_filter = ['status', 'direction']
    search_fields = ['session_id', 'customer__phone_number', 'dialer_call_id']
    readonly_fields = ['session_id', 'started_at', 'answered_at', 'ended_at',
                       'duration_seconds', 'total_cost', 'created_at', 'updated_at']
    date_hierarchy = 'started_at'


@admin.register(ConversationTurn)
class ConversationTurnAdmin(admin.ModelAdmin):
    list_display = ['call_session', 'speaker', 'text', 'intent', 'confidence', 'timestamp']
    list_filter = ['speaker']
    search_fields = ['text', 'call_session__session_id', 'intent']
    readonly_fields = ['timestamp', 'created_at', 'updated_at']
    date_hierarchy = 'timestamp'


# ═══════════════════════════════════════════════════════════════
# KNOWLEDGE
# ═══════════════════════════════════════════════════════════════

@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'doc_id', 'category', 'status', 'chunk_count', 'indexed_at']
    list_filter = ['category', 'status']
    search_fields = ['title', 'doc_id', 'content']
    readonly_fields = ['chunk_count', 'indexed_at', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'


# ═══════════════════════════════════════════════════════════════
# AI CONFIGURATION
# ═══════════════════════════════════════════════════════════════

@admin.register(TTSVoice)
class TTSVoiceAdmin(admin.ModelAdmin):
    list_display = ['voice_name', 'gender', 'provider_name', 'language', 'is_active', 'created_at']
    list_filter = ['provider_name', 'gender', 'language', 'is_active']
    search_fields = ['voice_name', 'provider_name', 'provider_voice_id']


@admin.register(LLMSetting)
class LLMSettingAdmin(admin.ModelAdmin):
    list_display = ['persona_name', 'name', 'provider', 'model', 'voice',
                     'language', 'is_active']
    list_filter = ['provider', 'language', 'voice', 'is_active']
    search_fields = ['persona_name', 'name', 'system_prompt']


# ═══════════════════════════════════════════════════════════════
# SYSTEM
# ═══════════════════════════════════════════════════════════════

@admin.register(ServiceErrorLog)
class ServiceErrorLogAdmin(admin.ModelAdmin):
    list_display = ['provider', 'stage', 'severity', 'error_type', 'session_id', 'created_at']
    list_filter = ['provider', 'severity']
    search_fields = ['error_type', 'error_message', 'session_id']
    readonly_fields = ['created_at']
    date_hierarchy = 'created_at'