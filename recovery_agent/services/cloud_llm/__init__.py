"""Cloud LLM package — BharatRouter (Gemma 4 31B) edition."""

from .client import BharatRouterClient

_client = None


def get_cloud_llm_client():
    global _client
    if _client is None:
        _client = BharatRouterClient()
    return _client