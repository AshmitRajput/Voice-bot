from django.urls import path
from voice_bot import views
from voice_bot import views_rag, views_admin, views_voice, views_intent_debug
urlpatterns = [
    # ========== CORE (views.py) ==========
    path('webhook/incoming/', views.webhook_incoming_call, name='incoming_call'),
    path('webhook/response/', views.process_customer_response, name='customer_response'),
    path('health/', views.health_check, name='health'),
    path('test/llm/', views.test_llm, name='test_llm'),
    path('stream/llm/chat/', views.stream_chat, name='stream_chat'),

    # ========== RAG (views_rag.py) ==========
    path('kb/store/', views_rag.kb_store),
    path('kb/ask/', views_rag.kb_ask),
    path('kb/ask/stream/', views_rag.kb_ask_stream),
    
    # Voice API
    path('voice/tts/', views_voice.tts_synthesize),
    path('kb/stats/', views_rag.kb_stats, name='kb_stats'),
    path('kb/documents/', views_rag.kb_get_all, name='kb_get_all'),
    path('kb/documents/<str:doc_id>/', views_rag.kb_delete, name='kb_delete'),
    path('kb/documents_by_id/<str:doc_id>/', views_rag.kb_get_by_id, name='kb_get_by_id'),
    path('kb/documents/<str:doc_id>/update/', views_rag.kb_update, name='kb_update'),

    # ========== ADMIN (views_admin.py) ==========
    path('admin-api/settings/', views_admin.admin_get_settings, name='admin_settings'),
    path('admin-api/settings/update', views_admin.admin_update_settings, name='admin_update_settings'),
    path('admin-api/voices/', views_admin.get_voice_options, name='voice_options'),
    path('admin-api/test-tts/', views_admin.admin_test_tts, name='admin_test_tts'),

    # ========== VOICE (views_voice.py) ==========
    path('voice/tts/', views_voice.tts_synthesize, name='tts_synthesize'),
    path('voice/plivo/call/', views_voice.plivo_call, name='plivo_call'),
    path('voice/plivo/answer/', views_voice.plivo_answer, name='plivo_answer'),
    path('voice/plivo/hangup/', views_voice.plivo_hangup, name='plivo_hangup'),
    path('voice/plivo/status/', views_voice.plivo_status, name='plivo_status'),

    # ========== SERVICE BOOKING (views.py) ==========
    path('crm/booking-availability/', views.booking_availability, name='crm_booking_availability'),
    path('crm/booking/create/', views.create_booking, name='crm_create_booking'),
    path('crm/call-summary/<uuid:session_id>/', views.get_call_summary, name='crm_call_summary'),
    path('crm/booking/cancel/', views.cancel_booking, name='crm_cancel_booking'),
    path('crm/bookings/', views.list_bookings, name='crm_list_bookings'),

    # ========== SEGMENT LLM SETTINGS (views_admin.py) ==========
    path('tts-voices/', views_admin.tts_voices),
    path('segments/', views_admin.segments),
    path('llm-settings/', views_admin.llm_settings),
    path('llm-settings/<int:segment_id>/', views_admin.llm_setting_detail),

    # ========== Knowledge Document ==============
    path('branches/', views_admin.dealer_branches, name='dealer_branches'),
    path('dealers/', views_admin.dealers_list, name='dealers_list'),   # NEW

    path("intent-debug/", views_intent_debug.intent_debug_page, name="intent_debug"),
    path("intent/", views_intent_debug.intent_api, name="intent_api"),
    path("intent/health/", views_intent_debug.intent_health, name="intent_health"),

    path('recordings/', views_admin.recordings),
    path('recordings/<int:session_id>/audio/', views_admin.recording_audio, name='recording_audio'),
    
    # ========== QUICK CALL (test page) ==========
    path('quick-call/', views_admin.quick_call_page, name='quick_call_page'),
    path('quick-call/meta/', views_admin.quick_call_meta, name='quick_call_meta'),
    path('quick-call/save/', views_admin.quick_call_save, name='quick_call_save'),
    path('quick-call/list/', views_admin.quick_call_list, name='quick_call_list'),

    # ========== QUICK VEHICLE (test page) ==========
    path('quick-vehicle/', views_admin.quick_vehicle_page, name='quick_vehicle_page'),
    path('quick-vehicle/customer-lookup/', views_admin.quick_vehicle_customer_lookup, name='quick_vehicle_customer_lookup'),
    path('quick-vehicle/save/', views_admin.quick_vehicle_save, name='quick_vehicle_save'),
    path('quick-vehicle/list/', views_admin.quick_vehicle_list, name='quick_vehicle_list'),
]