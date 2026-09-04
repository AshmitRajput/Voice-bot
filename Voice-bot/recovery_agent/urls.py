"""
URL routing for the recovery_agent app (local testing). """

from django.urls import path
from recovery_agent import views

urlpatterns = [
    # Health check
    path('health/', views.health_check, name='health'),

    # Test endpoints
    path('test/process-turn/', views.test_process_turn, name='test_process_turn'),
    path('test/classify-intent/', views.test_classify_intent, name='test_classify_intent'),
    path('test/tool-call/', views.test_tool_call, name='test_tool_call'),
    path('test/list-tools/', views.test_list_tools, name='test_list_tools'),
    path('test/state/', views.test_get_state, name='test_get_state'),
    path('test/clear-session/', views.test_clear_session, name='test_clear_session'),
]