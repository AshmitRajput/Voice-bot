from django.db import models


class CallSession(models.Model):
    """Minimal placeholder — one row per outbound call attempt."""
    call_sid = models.CharField(max_length=128, unique=True)
    customer_phone = models.CharField(max_length=20)
    module = models.CharField(
        max_length=32,
        choices=[
            ("service_reminder", "Service Reminder"),
            ("feedback_nps", "Feedback / NPS"),
            ("enquiry_followup", "Enquiry Follow-up"),
        ],
    )
    status = models.CharField(max_length=32, default="in_progress")
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.call_sid} ({self.module})"