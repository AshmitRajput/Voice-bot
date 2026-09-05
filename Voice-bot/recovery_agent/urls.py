"""
URL routing for the recovery_agent app (local testing + auth + admin API + RAG).
"""

from django.urls import path
from recovery_agent import views
from recovery_agent import views_auth
from recovery_agent import views_admin
from recovery_agent import views_rag


def _kb_document_dispatch(request, doc_id):
    """kb_document_detail/update/delete all live at the same URL per
    views_rag.py's own docstrings (GET/PUT/DELETE on /api/kb/documents/<doc_id>/).
    Each target function already has its own @require_http_methods, so an
    unsupported method still gets Django's normal 405 from that decorator."""
    if request.method == 'PUT':
        return views_rag.kb_document_update(request, doc_id)
    if request.method == 'DELETE':
        return views_rag.kb_document_delete(request, doc_id)
    return views_rag.kb_document_detail(request, doc_id)

urlpatterns = [
    # Health check
    path('health/', views.health_check, name='health'),

    # Auth (session-cookie based — single admin user)
    path('auth/csrf/', views_auth.auth_csrf, name='auth_csrf'),
    path('auth/login/', views_auth.auth_login, name='auth_login'),
    path('auth/logout/', views_auth.auth_logout, name='auth_logout'),
    path('auth/me/', views_auth.auth_me, name='auth_me'),

    # Test endpoints
    path('test/process-turn/', views.test_process_turn, name='test_process_turn'),
    path('test/classify-intent/', views.test_classify_intent, name='test_classify_intent'),
    path('test/tool-call/', views.test_tool_call, name='test_tool_call'),
    path('test/list-tools/', views.test_list_tools, name='test_list_tools'),
    path('test/state/', views.test_get_state, name='test_get_state'),
    path('test/clear-session/', views.test_clear_session, name='test_clear_session'),

    # ─── Admin: Dashboard / analytics ───────────────────────────
    path('admin/recovery/dashboard/', views_admin.recovery_dashboard, name='recovery_dashboard'),

    # ─── Admin: Customers ────────────────────────────────────────
    path('admin/customers/', views_admin.customers_list, name='customers_list'),
    path('admin/customers/<int:customer_id>/', views_admin.customer_detail, name='customer_detail'),

    # ─── Admin: Recovery campaigns ───────────────────────────────
    path('admin/campaigns/', views_admin.campaigns, name='campaigns'),
    path('admin/campaigns/<int:campaign_id>/', views_admin.campaign_detail, name='campaign_detail'),

    # ─── Admin: Recovery cases / callbacks ───────────────────────
    path('admin/recovery/cases/', views_admin.recovery_cases, name='recovery_cases'),
    path('admin/recovery/callbacks/', views_admin.recovery_callbacks, name='recovery_callbacks'),

    # ─── Admin: Call recordings / transcripts  ──────────
    path('admin/recordings/', views_admin.recordings, name='recordings'),
    path('admin/recordings/<str:session_id>/audio/', views_admin.recording_audio, name='recording_audio'),
    
    # ─── Admin: TTS voices ──────────────────────────────
    path('admin/tts-voices/', views_admin.tts_voices, name='tts_voices'),
    path('admin/tts-voices/<int:voice_id>/', views_admin.tts_voice_detail, name='tts_voice_detail'),
    path('admin/voice-options/', views_admin.get_voice_options, name='get_voice_options'),
    path('admin/test-tts/', views_admin.admin_test_tts, name='admin_test_tts'),

    # ─── Admin: LLM / persona settings ──────────────────
    path('admin/llm-settings/', views_admin.llm_settings, name='llm_settings'),
    path('admin/llm-settings/<int:setting_id>/', views_admin.llm_setting_detail, name='llm_setting_detail'),
    path('admin/call-settings/', views_admin.call_settings, name='call_settings'),
    # ─── Knowledge Base / RAG ────────────────────────────

    path('kb/store/', views_rag.kb_store, name='kb_store'),
    path('kb/ask/', views_rag.kb_ask, name='kb_ask'),
    path('kb/ask/stream/', views_rag.kb_ask_stream, name='kb_ask_stream'),
    path('kb/stats/', views_rag.kb_stats, name='kb_stats'),
    path('kb/documents/', views_rag.kb_documents, name='kb_documents'),
    path('kb/documents/<str:doc_id>/', _kb_document_dispatch, name='kb_document_dispatch'),

    # ─── Plivo outbound calling ──────────────────────────
    path('admin/calls/trigger/', views.trigger_call, name='trigger_call'),
    path('voice/plivo/answer/', views.plivo_answer, name='plivo_answer'),
    path('voice/plivo/hangup/', views.plivo_hangup, name='plivo_hangup'),
    path('admin/calls/<str:session_id>/', views_admin.call_detail_admin, name='call_detail_admin'),
]