import os
import sys
from django.apps import AppConfig


class RecoveryAgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "recovery_agent"

    def ready(self):
        # Skip RAG/LLM init for management commands
        skip_commands = {
            "makemigrations", "migrate", "collectstatic",
            "shell", "test", "createsuperuser",
            "showmigrations", "check",
        }
        if any(cmd in sys.argv for cmd in skip_commands):
            return
        # Don't preload anything yet -- lazy load on first use