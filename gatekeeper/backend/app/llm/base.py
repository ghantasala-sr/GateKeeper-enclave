"""The model-provider interface: everything the agent knows about a chat model.

The agent speaks only these neutral types. Each adapter converts them to and
from one provider's API, so the security code never touches a provider SDK.

Providers require the model's previous turn to be sent back *unchanged* in a
tool-calling conversation (Claude's thinking blocks, Gemini's thought
signatures), so an assistant Turn keeps the provider's own form in `native`
and the adapter replays it as-is.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Stop = Literal["end", "tool_use", "refusal", "max_tokens"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any  # as the model sent it; the agent validates it, adapters don't


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    provider: str | None = None  # which adapter produced `native`
    native: Any = None           # that provider's own form of this assistant turn


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: Stop
    raw_stop: str            # the provider's own stop/finish reason, for the trace
    model: str
    provider: str
    native: Any = None
    usage: dict = field(default_factory=dict)  # {"input_tokens": n, "output_tokens": n}

    def as_turn(self) -> Turn:
        return Turn(role="assistant", text=self.text, tool_calls=self.tool_calls,
                    provider=self.provider, native=self.native)


class ModelUnavailable(Exception):
    """The model can't be used right now: not configured, unreachable, rejected credentials, rate limited."""


class ChatModel(Protocol):
    provider: str
    model: str

    def unavailable_reason(self) -> str | None:
        """Why this model can't be called (missing configuration), or None if it can."""

    async def complete(self, *, system: str, turns: list[Turn], tools: list[ToolSpec]) -> ModelReply:
        """One model call. Raises ModelUnavailable for anything that isn't the caller's fault."""
