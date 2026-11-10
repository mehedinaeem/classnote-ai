"""Single provider selection point; callers depend on the generic contract."""
from .interfaces import LLMClient
from .ollama_client import OllamaClient


def get_llm_client() -> LLMClient:
    return OllamaClient()
