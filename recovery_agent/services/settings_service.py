"""
Settings logic - alag file mein
Model se separate, reusable
"""
from voice_bot.models import Dealer

def get_ai_settings():
    """Current settings lo"""
    obj, created = Dealer.objects.get_or_create(
        pk=1,
        defaults={
            'active_prompt': 'Tum OM Honda ki AI assistant ho...',
            'tts_provider': 'google',
            'tts_voice': 'hi-IN-Wavenet-A'
        }
    )
    return {
        'active_prompt': obj.active_prompt,
        'tts_provider': obj.tts_provider,
        'tts_voice': obj.tts_voice,
        'updated_at': obj.updated_at
    }

def update_ai_settings(active_prompt=None, tts_provider=None, tts_voice=None):
    """Settings update karo"""
    obj, _ = Dealer.objects.get_or_create(pk=1)
    
    if active_prompt is not None:
        obj.active_prompt = active_prompt
    if tts_provider is not None:
        obj.tts_provider = tts_provider
    if tts_voice is not None:
        obj.tts_voice = tts_voice
    
    obj.save()
    return get_ai_settings()