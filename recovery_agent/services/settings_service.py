"""
settings_service.py

Thin wrapper over LLMSetting (recovery-agent AI configuration). Replaces
the old Dealer.active_prompt/tts_provider/tts_voice fields entirely --
those lived on a Honda-era model that no longer exists.

get_or_create's `defaults` only apply the first time a row is created for
a given pk. In production you'll normally have exactly one active
LLMSetting row (or select by e.g. workflow="revenue_recovery"); adjust the
lookup below if you end up with multiple configs (per-segment, per-campaign
etc.) later.
"""
from recovery_agent.models import LLMSetting

_DEFAULT_SYSTEM_PROMPT = (
    "You are RecoverAI, an outbound revenue-recovery voice agent. "
    "Be professional, concise, and respectful. Never invent payment status, "
    "amounts, or links -- always use verified data. Ask at most one "
    "question at a time."
)


def get_ai_settings():
    """Current active LLM/voice settings."""
    obj, created = LLMSetting.objects.get_or_create(
        pk=1,
        defaults={
            "system_prompt": _DEFAULT_SYSTEM_PROMPT,
        },
    )
    return {
        "system_prompt": obj.system_prompt,
        "provider": getattr(obj, "provider", None),
        "model": getattr(obj, "model", None),
        "voice_id": getattr(obj, "voice_id", None),
        "updated_at": obj.updated_at,
    }


def update_ai_settings(system_prompt=None, provider=None, model=None, voice_id=None):
    """Update the active LLM/voice settings."""
    obj, _ = LLMSetting.objects.get_or_create(pk=1)

    if system_prompt is not None:
        obj.system_prompt = system_prompt
    if provider is not None:
        obj.provider = provider
    if model is not None:
        obj.model = model
    if voice_id is not None:
        obj.voice_id = voice_id

    obj.save()
    return get_ai_settings()