from django.contrib import admin
from django.utils.html import format_html
import json

from .models import (
    # Organisation
    Dealer, Branch, BranchHoliday, StaffUser,
    # Customer & Vehicle
    Customer, Vehicle, VehicleServiceRecord,
    # Segment / Agent / Knowledge
    TTSVoice, Segment, SegmentIntent,
    KnowledgeCollection, KnowledgeDocument, LLMSetting,
    # Import
    CsvStats, CsvDetails, CsvSegmentData,
    # Campaign & queue
    Campaign, CampaignBatch, CallTask, LiveCall,
    # Call data
    CallSession, ConversationTurn, CorrectIntent,
    # Appointment & rules
    Appointment, ServiceSchedule, ServiceErrorLog,
)


# ═══════════════════════════════════════════════════════════════
# 1. ORGANISATION
# ═══════════════════════════════════════════════════════════════

@admin.register(Dealer)
class DealerAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'city', 'main_branch', 'daily_call_budget',
                    'max_concurrent_calls', 'llm_provider', 'is_active', 'flag']
    list_filter = ['is_active', 'flag', 'llm_provider']
    search_fields = ['name', 'code', 'city']
    fieldsets = (
        ('Identity', {'fields': ('name', 'code', 'city', 'phone', 'email', 'main_branch')}),
        ('Calling capacity', {
            'fields': ('daily_call_budget', 'max_concurrent_calls',
                       'min_days_between_calls', 'max_calls_per_customer_month'),
            'description': "daily_call_budget = roz total call. Campaigns ke "
                           "daily_call_limit ka sum isse validate hota hai.",
        }),
        ('AI providers', {'fields': ('stt_provider', 'tts_provider', 'llm_provider',
                                     'llm_model', 'rag_enabled', 'rag_distance_threshold')}),
        ('Status', {'fields': ('is_active', 'flag')}),
    )


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'network_code', 'dealer', 'opening_time',
                    'closing_time', 'max_per_slot', 'is_main_branch', 'is_active', 'flag']
    list_filter = ['dealer', 'is_main_branch', 'is_active', 'flag']
    search_fields = ['name', 'code', 'network_code', 'city']
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'name', 'code', 'network_code',
                                 'is_main_branch', 'address', 'city', 'phone')}),
        ('Slot config', {
            'fields': ('opening_time', 'closing_time', 'slot_duration_minutes',
                       'max_per_slot', 'weekly_off'),
            'description': "weekly_off: 0=Mon … 6=Sun. [6] = Sunday band. [] = roz khula.",
        }),
        ('Escalation', {'fields': ('advisor_phone',)}),
        ('Status', {'fields': ('is_active', 'flag')}),
    )


@admin.register(BranchHoliday)
class BranchHolidayAdmin(admin.ModelAdmin):
    list_display = ['branch', 'holiday_date', 'reason', 'flag']
    list_filter = ['dealer', 'branch', 'holiday_date']
    search_fields = ['reason']
    date_hierarchy = 'holiday_date'


@admin.register(StaffUser)
class StaffUserAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'role', 'dealer', 'branch',
                    'can_edit_knowledge', 'can_edit_agent', 'can_edit_campaign',
                    'can_view_all_branches', 'is_active']
    list_filter = ['role', 'dealer', 'branch', 'is_active', 'flag']
    search_fields = ['name', 'email', 'phone']
    readonly_fields = ['last_login_at']
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'branch', 'name', 'email', 'phone',
                                 'password_hash', 'role')}),
        ('Permissions', {
            'fields': ('can_edit_knowledge', 'can_edit_agent', 'can_edit_campaign',
                       'can_view_all_branches'),
            'description': "KB aur Agent SAB branch ke bot ko affect karte hain — "
                           "sirf owner/dealer_admin ko dena chahiye.",
        }),
        ('Status', {'fields': ('is_active', 'last_login_at', 'flag')}),
    )


# ═══════════════════════════════════════════════════════════════
# 2. CUSTOMER & VEHICLE
# ═══════════════════════════════════════════════════════════════

class VehicleInline(admin.TabularInline):
    model = Vehicle
    extra = 0
    fields = ['vehicle_name', 'registration_no', 'chassis_no',
              'next_service_type_raw', 'next_service_due_date', 'on_reminder']
    show_change_link = True


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['phone_number', 'name', 'dealer', 'default_branch', 'city',
                    'do_not_call', 'calls_this_month', 'last_called_at', 'created_at']
    list_filter = ['dealer', 'default_branch', 'do_not_call', 'crm_call_status',
                   'flag', 'created_at']
    search_fields = ['phone_number', 'alt_phone', 'name', 'email']
    readonly_fields = ['last_called_at', 'total_calls', 'calls_this_month',
                       'calls_month_reset_at', 'created_at', 'updated_at']
    inlines = [VehicleInline]
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'default_branch', 'phone_number', 'alt_phone',
                                 'name', 'email', 'city', 'address', 'preferred_language')}),
        ('Sales context', {'fields': ('preferred_model', 'budget_range'),
                           'classes': ('collapse',)}),
        ('Calling control', {
            'fields': ('do_not_call', 'do_not_call_at', 'do_not_call_reason',
                       'crm_call_status', 'last_called_at', 'calls_this_month',
                       'calls_month_reset_at', 'total_calls'),
            'description': "do_not_call = har campaign ise honour karega (TRAI/DND).",
        }),
        ('Metrics', {'fields': ('total_spend', 'csat_score')}),
        ('Status', {'fields': ('flag', 'created_at', 'updated_at')}),
    )


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    """Sabse main table — call ka decision isi row se hota hai."""
    list_display = ['chassis_no', 'reg_display', 'vehicle_name', 'customer',
                    'next_service_type_raw', 'next_service_due_date',
                    'insurance_expiry_date', 'amc_expiry_date', 'on_reminder']
    list_filter = ['dealer', 'purchased_branch', 'last_service_branch',
                   'next_service_type_raw', 'registration_no_valid',
                   'on_reminder', 'is_sold_off', 'flag']
    search_fields = ['chassis_no', 'registration_no', 'engine_no',
                     'customer__phone_number', 'customer__name', 'vehicle_name']
    date_hierarchy = 'next_service_due_date'
    readonly_fields = ['registration_no_valid', 'created_at', 'updated_at']
    fieldsets = (
        ('Owner', {'fields': ('dealer', 'customer', 'purchased_branch',
                              'last_service_branch')}),
        ('Vehicle', {
            'fields': ('vehicle_name', 'vehicle_model', 'chassis_no',
                       'registration_no', 'registration_no_valid', 'engine_no',
                       'color', 'purchase_date', 'last_km'),
            'description': "chassis_no = Frame Number, PRIMARY dedup key. "
                           "registration_no sirf display (30% data kachra hai).",
        }),
        ('Service cycle', {
            'fields': ('last_service_type', 'last_service_type_raw', 'last_service_date',
                       'next_service_type', 'next_service_type_raw',
                       'next_service_due_date', 'missed_service_date'),
            'description': "next_* fields CSV se aate hain — hum calculate nahi karte. "
                           "next_service_type_raw se segment match hota hai.",
        }),
        ('Dealer codes (CRM raw)', {
            'fields': ('selling_dealer_code', 'last_service_dealer_code'),
            'classes': ('collapse',),
        }),
        ('Insurance', {'fields': ('insurance_provider', 'insurance_policy_no',
                                  'insurance_expiry_date')}),
        ('AMC & coverage', {'fields': ('amc_plan', 'amc_expiry_date',
                                       'eha_end_date', 'ew_end_date', 'rsa_end_date')}),
        ('Reminder control', {'fields': ('on_reminder', 'is_sold_off', 'flag')}),
    )

    @admin.display(description='Registration')
    def reg_display(self, obj):
        return obj.registration_no if obj.registration_no_valid else '—'


@admin.register(VehicleServiceRecord)
class VehicleServiceRecordAdmin(admin.ModelAdmin):
    """Jo service HO CHUKI — history. Appointment (booking) se alag."""
    list_display = ['vehicle', 'customer', 'service_type', 'service_date',
                    'branch', 'km_reading', 'amount', 'source']
    list_filter = ['dealer', 'branch', 'service_type', 'source', 'service_date', 'flag']
    search_fields = ['vehicle__chassis_no', 'vehicle__registration_no',
                     'customer__phone_number', 'customer__name']
    date_hierarchy = 'service_date'
    readonly_fields = ['created_at']


# ═══════════════════════════════════════════════════════════════
# 3. SEGMENT / AGENT / KNOWLEDGE
# ═══════════════════════════════════════════════════════════════

@admin.register(TTSVoice)
class TTSVoiceAdmin(admin.ModelAdmin):
    list_display = ['voice_name', 'voice_code', 'gender', 'language',
                    'provider_name', 'dealer', 'is_active', 'updated_at']
    list_filter = ['gender', 'provider_name', 'language', 'is_active', 'flag']
    search_fields = ['voice_name', 'voice_code', 'provider_name']


class SegmentIntentInline(admin.TabularInline):
    model = SegmentIntent
    extra = 0
    fields = ['intent_code', 'display_name', 'action_type',
              'is_outcome', 'is_terminal', 'priority', 'is_active']
    show_change_link = True


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    """FLAT — 7 peer segments, koi parent/sub-segment nahi."""
    list_display = ['name', 'slug', 'dealer', 'module', 'trigger_type',
                    'match_service_type', 'days_before', 'days_after',
                    'cached_count', 'cached_due_today', 'conversion_rate']
    list_filter = ['dealer', 'module', 'trigger_type', 'flag']
    search_fields = ['name', 'slug', 'description', 'match_service_type']
    readonly_fields = ['cached_count', 'cached_due_today', 'conversion_rate']
    inlines = [SegmentIntentInline]
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'name', 'slug', 'description', 'module')}),
        ('Rule', {
            'fields': ('trigger_type', 'match_service_type', 'days_before',
                       'days_after', 'extra_rule'),
            'description': "match_service_type = CSV ka 'Next Service Type' "
                           "(FREE 01 / FREE 02 / FREE 03 / PAID). "
                           "trigger_type code me mapped hai — badalna mat.",
        }),
        ('Cached stats (nightly)', {
            'fields': ('cached_count', 'cached_due_today', 'conversion_rate', 'accuracy'),
        }),
        ('Status', {'fields': ('flag',)}),
    )


@admin.register(SegmentIntent)
class SegmentIntentAdmin(admin.ModelAdmin):
    list_display = ['intent_code', 'display_name', 'segment', 'action_type',
                    'is_outcome', 'is_terminal', 'priority', 'hit_count', 'is_active']
    list_filter = ['dealer', 'segment', 'action_type', 'is_outcome',
                   'is_terminal', 'is_active', 'flag']
    search_fields = ['intent_code', 'display_name', 'segment__name']
    readonly_fields = ['hit_count']
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'segment', 'intent_code', 'display_name')}),
        ('Training data', {'fields': ('sample_utterances',),
                           'description': 'JSON: ["service karani hai", "kal aa jaunga"]'}),
        ('Behaviour', {
            'fields': ('action_type', 'response_template', 'is_terminal',
                       'is_outcome', 'priority'),
            'description': "is_outcome=True matlab CallSession.final_intent me aa sakta hai.",
        }),
        ('Stats', {'fields': ('accuracy', 'hit_count', 'is_active', 'flag')}),
    )


class KnowledgeDocumentInline(admin.TabularInline):
    model = KnowledgeDocument
    extra = 0
    fields = ['title', 'category', 'chunk_count', 'status', 'indexed_at']
    readonly_fields = ['chunk_count', 'indexed_at']
    show_change_link = True


@admin.register(KnowledgeCollection)
class KnowledgeCollectionAdmin(admin.ModelAdmin):
    """KB ka bundle — segments se tag hota hai (M2M)."""
    list_display = ['name', 'slug', 'dealer', 'segment_list',
                    'collection_name', 'doc_count', 'chunk_count', 'flag']
    list_filter = ['dealer', 'segments', 'flag']
    search_fields = ['name', 'slug', 'collection_name']
    filter_horizontal = ['segments']
    readonly_fields = ['doc_count', 'chunk_count']
    inlines = [KnowledgeDocumentInline]
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'name', 'slug', 'description',
                                 'collection_name')}),
        ('Scope', {
            'fields': ('segments', 'branch_ids'),
            'description': "Ek document kai segments me ho sakta hai — Service ke liye "
                           "likha jo Insurance me bhi chahiye, dono select kar do. "
                           "branch_ids: [] = sab branch.",
        }),
        ('Stats', {'fields': ('doc_count', 'chunk_count', 'flag')}),
    )

    @admin.display(description='Segments')
    def segment_list(self, obj):
        names = list(obj.segments.values_list('name', flat=True)[:4])
        more = obj.segments.count() - len(names)
        return ", ".join(names) + (f" +{more}" if more > 0 else "") or "—"


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ['title', 'collection', 'category', 'dealer',
                    'chunk_count', 'status', 'indexed_at']
    list_filter = ['dealer', 'collection', 'category', 'status', 'flag']
    search_fields = ['title', 'doc_id', 'content', 'source']
    filter_horizontal = ['segments']
    readonly_fields = ['chunk_count', 'indexed_at', 'created_at', 'updated_at']
    fieldsets = (
        ('Scope', {
            'fields': ('dealer', 'collection', 'segments', 'branch_ids'),
            'description': "segments khali chhodo to collection ka tagging use hoga.",
        }),
        ('Document', {'fields': ('doc_id', 'title', 'category', 'source',
                                 'content', 'metadata')}),
        ('Index status', {'fields': ('chunk_count', 'status', 'indexed_at', 'flag')}),
    )


@admin.register(LLMSetting)
class LLMSettingAdmin(admin.ModelAdmin):
    """AI Agent — 3 hain (Service / Insurance / AMC). KB yahan attach nahi hoti."""
    list_display = ['agent_name', 'persona_name', 'module', 'dealer', 'voice',
                    'status', 'version', 'total_calls', 'connect_rate',
                    'booking_rate', 'updated_at']
    list_filter = ['dealer', 'module', 'status', 'allow_customer_barge_in',
                   'voice__gender', 'voice__provider_name', 'flag']
    search_fields = ['agent_name', 'persona_name', 'voice__voice_name']
    readonly_fields = ['total_calls', 'connect_rate', 'booking_rate', 'intent_accuracy']
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'agent_name', 'module')}),
        ('Persona & Voice', {
            'fields': ('persona_name', 'voice', 'opening_line', 'closing_line',
                       'system_prompt', 'behaviour'),
            'description': "Campaign.opening_line / extra_prompt inhe override/extend "
                           "karte hain. Vars: {customer_name} {branch_name} "
                           "{vehicle_model} {due_date}",
        }),
        ('Tuning', {
            'fields': ('tone', 'pace', 'persistence', 'barge_in_threshold'),
            'description': "persistence HIGH mat rakhna — customer chid ke "
                           "do_not_call karva dega, jo permanent hai.",
        }),
        ('Conversation control', {'fields': ('max_turns', 'allow_customer_barge_in')}),
        ('Status', {'fields': ('status', 'version', 'flag')}),
        ('Live stats', {'fields': ('total_calls', 'connect_rate', 'booking_rate',
                                   'intent_accuracy')}),
    )


# ═══════════════════════════════════════════════════════════════
# 4. DATA IMPORT
# ═══════════════════════════════════════════════════════════════

@admin.register(CsvStats)
class CsvStatsAdmin(admin.ModelAdmin):
    """Ek uploaded file = ek row. Sirf counts + status."""
    list_display = ['file_name', 'branch', 'list_type', 'period_month', 'status',
                    'total_rows', 'segment_data_created', 'unmatched_count',
                    'skipped_count', 'failed_count', 'reconcile_flag', 'created_at']
    list_filter = ['dealer', 'branch', 'list_type', 'status', 'period_month', 'flag']
    search_fields = ['file_name', 'sheet_name']
    date_hierarchy = 'created_at'
    readonly_fields = ['total_rows', 'customers_created', 'customers_updated',
                       'vehicles_created', 'vehicles_updated', 'segment_data_created',
                       'unmatched_count', 'skipped_count', 'failed_count',
                       'unmatched_pretty', 'column_map_pretty', 'error_log_pretty',
                       'preview_pretty', 'reconcile_flag',
                       'started_at', 'finished_at', 'created_at']
    fieldsets = (
        ('File', {'fields': ('dealer', 'branch', 'list_type', 'period_month',
                             'file_name', 'file_path', 'sheet_name', 'uploaded_by')}),
        ('Reconciliation', {
            'fields': ('total_rows', 'segment_data_created', 'unmatched_count',
                       'skipped_count', 'failed_count', 'reconcile_flag'),
            'description': "total_rows = segment_data + unmatched + skipped + failed "
                           "hona chahiye. Match na kare to bug hai.",
        }),
        ('Created / updated', {'fields': ('customers_created', 'customers_updated',
                                          'vehicles_created', 'vehicles_updated')}),
        ('Details', {'fields': ('unmatched_pretty', 'column_map_pretty',
                                'preview_pretty', 'error_log_pretty')}),
        ('Timing', {'fields': ('status', 'started_at', 'finished_at',
                               'created_at', 'flag')}),
    )

    @admin.display(description='Reconciles?', boolean=True)
    def reconcile_flag(self, obj):
        return obj.reconciles

    @admin.display(description='Unmatched types')
    def unmatched_pretty(self, obj):
        return self._json(obj.unmatched_types)

    @admin.display(description='Column map')
    def column_map_pretty(self, obj):
        return self._json(obj.column_map)

    @admin.display(description='Errors')
    def error_log_pretty(self, obj):
        return self._json(obj.error_log)

    @admin.display(description='Preview')
    def preview_pretty(self, obj):
        return self._json(obj.preview_data)

    @staticmethod
    def _json(data):
        if not data:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;max-height:400px;overflow:auto;margin:0">{}</pre>',
            json.dumps(data, indent=2, ensure_ascii=False),
        )


@admin.register(CsvDetails)
class CsvDetailsAdmin(admin.ModelAdmin):
    """CSV ki har line as-is. Yahan koi validation nahi — raw JSON."""
    list_display = ['row_number', 'csv_stats', 'phone_clean', 'frame_no',
                    'next_service_type_raw', 'crm_call_status',
                    'process_status', 'error']
    list_filter = ['dealer', 'csv_stats', 'process_status',
                   'next_service_type_raw', 'crm_call_status', 'flag']
    search_fields = ['phone_raw', 'phone_clean', 'frame_no', 'registration_raw']
    readonly_fields = ['raw_pretty', 'created_at']
    fields = ['dealer', 'csv_stats', 'row_number', 'raw_pretty', 'raw',
              'phone_raw', 'phone_clean', 'frame_no', 'registration_raw',
              'next_service_type_raw', 'next_service_date', 'crm_call_status',
              'process_status', 'error', 'customer', 'vehicle', 'segment_data',
              'flag', 'created_at']

    @admin.display(description='Raw row')
    def raw_pretty(self, obj):
        if not obj.raw:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;max-height:400px;overflow:auto;margin:0">{}</pre>',
            json.dumps(obj.raw, indent=2, ensure_ascii=False, default=str),
        )


@admin.register(CsvSegmentData)
class CsvSegmentDataAdmin(admin.ModelAdmin):
    """Processed rows — nightly scheduler yahi se uthata hai."""
    list_display = ['customer', 'vehicle', 'segment', 'batch', 'branch',
                    'due_date', 'is_queued', 'queued_at', 'flag']
    list_filter = ['dealer', 'segment', 'branch', 'is_queued', 'flag', 'due_date']
    search_fields = ['customer__phone_number', 'customer__name',
                     'vehicle__chassis_no', 'vehicle__registration_no']
    date_hierarchy = 'due_date'
    readonly_fields = ['queued_at', 'created_at']


# ═══════════════════════════════════════════════════════════════
# 5. CAMPAIGN & BATCH
# ═══════════════════════════════════════════════════════════════

class CampaignBatchInline(admin.TabularInline):
    model = CampaignBatch
    extra = 0
    fields = ['period', 'is_current', 'total_customers', 'total_queued',
              'total_called', 'total_connected', 'total_booked']
    readonly_fields = ['total_customers', 'total_queued', 'total_called',
                       'total_connected', 'total_booked']
    ordering = ['-period']
    show_change_link = True


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    """
    Control panel — 1 segment = 1 campaign, PERMANENT.
    Nayi CSV pe duplicate nahi banti, sirf CampaignBatch nayi banti hai.
    """
    list_display = ['name', 'segment', 'agent', 'branch', 'is_active', 'status',
                    'daily_call_limit', 'priority', 'call_start_time',
                    'call_end_time', 'total_called', 'total_booked']
    list_filter = ['dealer', 'branch', 'is_active', 'status', 'agent', 'flag']
    search_fields = ['name', 'description', 'segment__name']
    list_editable = ['is_active', 'daily_call_limit']
    readonly_fields = ['total_called', 'total_connected', 'total_booked', 'revenue']
    inlines = [CampaignBatchInline]
    fieldsets = (
        ('Identity', {'fields': ('dealer', 'branch', 'name', 'description')}),
        ('Kisko & Kaun', {
            'fields': ('segment', 'agent'),
            'description': "segment = KISKO call karna hai (1:1). "
                           "agent = KAUN bolega (5 campaigns ek Service Agent share karte hain).",
        }),
        ('Content override', {
            'fields': ('opening_line', 'extra_prompt'),
            'description': "Khali = agent ki default line. "
                           "Vars: {customer_name} {branch_name} {vehicle_model} {due_date}",
        }),
        ('Controls', {
            'fields': ('is_active', 'status', 'daily_call_limit', 'min_daily_calls',
                       'call_start_time', 'call_end_time', 'call_days',
                       'max_attempts', 'retry_gap_days', 'priority', 'channel',
                       'start_date', 'end_date'),
            'description': "Sab active campaigns ke daily_call_limit ka sum "
                           "Dealer.daily_call_budget se zyada nahi hona chahiye.",
        }),
        ('Lifetime stats', {'fields': ('total_called', 'total_connected',
                                       'total_booked', 'revenue')}),
        ('Meta', {'fields': ('created_by', 'flag')}),
    )


@admin.register(CampaignBatch)
class CampaignBatchAdmin(admin.ModelAdmin):
    """Ek campaign ka ek mahine ka data + results."""
    list_display = ['campaign', 'segment', 'period', 'is_current', 'branch',
                    'total_customers', 'total_queued', 'total_called',
                    'total_connected', 'total_booked', 'revenue']
    list_filter = ['dealer', 'campaign', 'segment', 'branch', 'is_current',
                   'period', 'flag']
    search_fields = ['campaign__name', 'segment__name']
    date_hierarchy = 'period'
    readonly_fields = ['total_customers', 'total_queued', 'total_called',
                       'total_connected', 'total_interested', 'total_booked',
                       'total_callback', 'total_failed', 'total_escalated',
                       'revenue', 'created_at', 'updated_at']


# ═══════════════════════════════════════════════════════════════
# 6. CALL QUEUE
# ═══════════════════════════════════════════════════════════════

@admin.register(CallTask)
class CallTaskAdmin(admin.ModelAdmin):
    list_display = ['customer', 'vehicle', 'reason', 'campaign', 'segment',
                    'status', 'scheduled_for', 'due_date', 'priority',
                    'attempts', 'last_outcome', 'skip_reason']
    list_filter = ['dealer', 'branch', 'status', 'campaign', 'segment',
                   'source', 'scheduled_for', 'flag']
    search_fields = ['customer__phone_number', 'customer__name',
                     'vehicle__chassis_no', 'vehicle__registration_no', 'reason']
    date_hierarchy = 'scheduled_for'
    readonly_fields = ['started_at', 'completed_at', 'call_session',
                       'created_at', 'updated_at']
    fieldsets = (
        ('Target', {'fields': ('dealer', 'branch', 'campaign', 'segment', 'batch',
                               'segment_data', 'customer', 'vehicle')}),
        ('Reason', {'fields': ('reason', 'due_date')}),
        ('Scheduling', {
            'fields': ('scheduled_for', 'scheduled_time', 'priority', 'source'),
            'description': "Callback pe NAYI row mat banao — isi ki scheduled_for "
                           "aage badhao.",
        }),
        ('Execution', {'fields': ('status', 'attempts', 'max_attempts', 'started_at',
                                  'completed_at', 'call_session', 'last_outcome',
                                  'skip_reason', 'flag')}),
    )


@admin.register(LiveCall)
class LiveCallAdmin(admin.ModelAdmin):
    """Abhi chal rahi calls. Call khatam pe row DELETE ho jaati hai."""
    list_display = ['customer', 'state', 'dealer', 'branch', 'campaign',
                    'last_intent', 'started_at', 'updated_at']
    list_filter = ['dealer', 'branch', 'state', 'campaign']
    search_fields = ['customer__phone_number', 'customer__name',
                     'session_id', 'dialer_call_id']
    readonly_fields = ['started_at', 'updated_at']


# ═══════════════════════════════════════════════════════════════
# 7. CALL DATA
# ═══════════════════════════════════════════════════════════════

@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'customer', 'vehicle', 'segment', 'campaign',
                    'batch', 'status', 'final_intent_code', 'duration_seconds',
                    'total_cost', 'started_at']
    list_filter = ['dealer', 'branch', 'status', 'segment', 'campaign', 'batch',
                   'agent', 'direction', 'started_at', 'flag']
    search_fields = ['session_id', 'customer__phone_number', 'customer__name',
                     'dialer_call_id', 'call_summary']
    date_hierarchy = 'started_at'
    readonly_fields = ['session_id', 'started_at', 'ended_at', 'duration_seconds',
                       'call_summary', 'accuracy', 'filler_accuracy', 'llm_accuracy',
                       'transcript_pretty', 'timing_pretty', 'total_cost']
    fieldsets = (
        ('Scope', {'fields': ('dealer', 'branch', 'session_id', 'customer', 'vehicle')}),
        ('Attribution', {'fields': ('segment', 'campaign', 'batch', 'agent', 'direction')}),
        ('Result', {
            'fields': ('status', 'final_intent', 'final_intent_code', 'intent_confidence'),
            'description': "status = TECHNICAL (lagi/uthi/kati). "
                           "final_intent = BUSINESS result (booked/declined/callback).",
        }),
        ('Timing', {'fields': ('started_at', 'ended_at', 'duration_seconds',
                               'avg_response_ms', 'started_at_epoch', 'ended_at_epoch')}),
        ('Cost', {'fields': ('llm_pricing', 'stt_pricing', 'tts_pricing',
                             'dialer_pricing', 'dialer_billed_seconds', 'total_cost')}),
        ('Quality', {'fields': ('accuracy', 'filler_accuracy', 'llm_accuracy')}),
        ('Content', {'fields': ('call_summary', 'transcript_pretty', 'transcript',
                                'intent_history', 'timing_pretty', 'timing_summary')}),
        ('Recording', {'fields': ('recording_stereo', 'recording_mixed')}),
        ('Escalation', {'fields': ('escalated_to', 'dialer_call_id', 'flag')}),
    )

    @admin.display(description='Transcript (readable)')
    def transcript_pretty(self, obj):
        if not obj.transcript:
            return "—"
        rows = []
        for t in obj.transcript:
            speaker = t.get('speaker', '?')
            colour = '#0b6' if speaker == 'customer' else '#06c'
            rows.append(
                f'<div style="margin:2px 0"><b style="color:{colour}">{speaker}:</b> '
                f'{t.get("text", "")}</div>'
            )
        return format_html('<div style="max-height:400px;overflow:auto">{}</div>',
                           format_html("".join(rows)))

    @admin.display(description='Timing summary')
    def timing_pretty(self, obj):
        if not obj.timing_summary:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;margin:0">{}</pre>',
            json.dumps(obj.timing_summary, indent=2, ensure_ascii=False),
        )


@admin.register(ConversationTurn)
class ConversationTurnAdmin(admin.ModelAdmin):
    # timing_preview: build_timing_record() ke saare 8 fields LIST view me hi
    # dikhte hain -- row kholne ki zarurat nahi.
    list_display = ['call_session', 'speaker', 'text', 'intent',
                    'timestamp', 'timing_preview']
    list_filter = ['dealer', 'speaker', 'flag']
    search_fields = ['call_session__session_id', 'text']
    readonly_fields = ['timing_pretty']
    fields = ['dealer', 'call_session', 'speaker', 'text', 'audio_url', 'intent',
              'filler_text', 'confidence', 'accuracy', 'filler_accuracy',
              'llm_pricing', 'stt_pricing', 'tts_pricing',
              'timing', 'timing_pretty', 'turn_start_seconds', 'turn_end_seconds',
              'timestamp', 'flag']

    @admin.display(description='Timing (all 8 fields)')
    def timing_preview(self, obj):
        if not obj.timing:
            return "—"
        # Jo 8 keys build_timing_record() hamesha likhta hai, usi order me.
        # Null bhi dikhte hain taaki pata chale kaunse genuinely missing hain.
        keys = [
            'stt_first_token', 'stt_complete', 'filler_play',
            'llm_first_token', 'tts_first_token', 'llm_complete',
            'filler_audio_at_ms', 'response_audio_at_ms',
        ]
        return format_html(
            '<span style="white-space:nowrap">{}</span>',
            " | ".join(f"{k}={obj.timing.get(k)}" for k in keys),
        )

    @admin.display(description='Timing (formatted JSON)')
    def timing_pretty(self, obj):
        if not obj.timing:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;margin:0">{}</pre>',
            json.dumps(obj.timing, indent=2, ensure_ascii=False),
        )


@admin.register(CorrectIntent)
class CorrectIntentAdmin(admin.ModelAdmin):
    """Auto-QA — fast classifier vs post-call review."""
    list_display = ['turn', 'conversation', 'intent', 'suggested_intent',
                    'matched', 'created_at']
    list_filter = ['dealer', 'intent', 'suggested_intent', 'created_at', 'flag']
    search_fields = ['customer_text', 'conversation__session_id']
    readonly_fields = ['created_at']

    @admin.display(description='Correct?', boolean=True)
    def matched(self, obj):
        return obj.intent == obj.suggested_intent


# ═══════════════════════════════════════════════════════════════
# 8. APPOINTMENT & RULES
# ═══════════════════════════════════════════════════════════════

@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    """BOOKING (bhavishya) — VehicleServiceRecord (bhoot) se alag."""
    list_display = ['customer', 'vehicle', 'branch', 'slot_date', 'slot_time',
                    'service_type', 'status', 'source', 'bay', 'advisor', 'created_at']
    list_filter = ['dealer', 'branch', 'status', 'service_type', 'source',
                   'slot_date', 'pickup_required', 'flag']
    search_fields = ['customer__phone_number', 'customer__name',
                     'vehicle__chassis_no', 'vehicle__registration_no']
    date_hierarchy = 'slot_date'
    readonly_fields = ['call_session', 'reminder_sent_at', 'completed_at',
                       'created_at', 'updated_at']
    fieldsets = (
        ('Who', {'fields': ('dealer', 'branch', 'customer', 'vehicle')}),
        ('Slot', {
            'fields': ('slot_date', 'slot_time', 'service_type', 'bay', 'advisor'),
            'description': "slot_time = 1-ghante slot ka START. "
                           "Ek slot me max Branch.max_per_slot customers.",
        }),
        ('Status', {'fields': ('status', 'source', 'completed_at', 'flag')}),
        ('Pickup & notes', {'fields': ('pickup_required', 'pickup_address', 'notes')}),
        ('Attribution', {'fields': ('call_session', 'campaign', 'batch',
                                    'reminder_sent_at')}),
    )


@admin.register(ServiceSchedule)
class ServiceScheduleAdmin(admin.ModelAdmin):
    """⚠️ Abhi UNUSED — CRM already Next Service Date deta hai."""
    list_display = ['dealer', 'vehicle_model', 'from_service', 'to_service',
                    'days_after', 'km_after', 'flag']
    list_filter = ['dealer', 'from_service', 'to_service', 'flag']
    search_fields = ['vehicle_model']


@admin.register(ServiceErrorLog)
class ServiceErrorLogAdmin(admin.ModelAdmin):
    list_display = ['provider', 'stage', 'severity', 'error_type',
                    'session_id', 'dealer', 'created_at']
    list_filter = ['provider', 'severity', 'dealer', 'created_at', 'flag']
    search_fields = ['session_id', 'error_type', 'error_message', 'stage']
    date_hierarchy = 'created_at'
    readonly_fields = ['context_pretty', 'created_at']
    fields = ['dealer', 'call_session', 'session_id', 'provider', 'stage',
              'severity', 'error_type', 'error_message', 'context',
              'context_pretty', 'created_at', 'flag']

    @admin.display(description='Context')
    def context_pretty(self, obj):
        if not obj.context:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;max-height:400px;overflow:auto;margin:0">{}</pre>',
            json.dumps(obj.context, indent=2, ensure_ascii=False, default=str),
        )