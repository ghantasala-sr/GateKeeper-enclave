"""Pick the chat model from LLM_PROVIDER. See base.py for the interface."""

from functools import lru_cache

from app.config import settings
from app.llm.base import ChatModel, ModelReply, ModelUnavailable, ToolCall, ToolResult, ToolSpec, Turn

__all__ = ["ChatModel", "ModelReply", "ModelUnavailable", "ToolCall", "ToolResult", "ToolSpec", "Turn", "get_model"]

PROVIDERS = ("vertex-gemini", "anthropic")


@lru_cache(maxsize=1)
def get_model() -> ChatModel:
    if settings.llm_provider == "vertex-gemini":
        from app.llm.vertex_gemini import VertexGeminiModel
        return VertexGeminiModel()
    if settings.llm_provider == "anthropic":
        from app.llm.anthropic import AnthropicModel
        return AnthropicModel()
    raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {settings.llm_provider!r}")
