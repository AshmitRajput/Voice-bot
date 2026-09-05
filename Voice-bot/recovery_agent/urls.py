"""
URL routing for the recovery_agent app (local testing + auth).
"""

from django.urls import path
from recovery_agent import views
from recovery_agent import views_auth

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
]