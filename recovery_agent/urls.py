from django.urls import path
from .views.call_flow import health_check, webhook_incoming_call, process_customer_response

urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("webhook/incoming-call/", webhook_incoming_call, name="webhook_incoming_call"),
    path("call/process-response/", process_customer_response, name="process_customer_response"),
]