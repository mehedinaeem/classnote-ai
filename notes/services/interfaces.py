"""Provider-neutral contract for future local LLM consumers."""
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


class LLMServiceError(Exception):
    """An expected local LLM configuration, transport, or response failure."""


@dataclass(frozen=True)
class LLMHealth:
    connected: bool
    model: str
    model_available: bool = False
    message: str = ""


class LLMClient(Protocol):
    model: str

    def health_check(self) -> LLMHealth: ...

    def chat(self, messages: Sequence[Mapping[str, str]], *, response_schema: dict | None = None) -> str: ...
