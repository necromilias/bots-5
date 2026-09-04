"""Generation backend adapters."""

from .fake import FakeStreamingBackend
from .openai_compatible import OpenAICompatibleStreamingBackend

__all__ = ["FakeStreamingBackend", "OpenAICompatibleStreamingBackend"]
