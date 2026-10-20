"""Claude via the Anthropic SDK. Used for local development only; the Enclave uses Vertex AI."""

import anthropic

from app.config import settings
from app.llm.base import ModelReply, ModelUnavailable, ToolCall, ToolSpec, Turn

PROVIDER = "anthropic"

_STOPS = {"tool_use": "tool_use", "refusal": "refusal", "max_tokens": "max_tokens"}


def render(turns: list[Turn]) -> list[dict]:
    messages = []
    for t in turns:
        if t.role == "user":
            messages.append({"role": "user", "content": t.text})
        elif t.role == "assistant":
            if t.provider == PROVIDER and t.native is not None:
                # Replay unchanged: thinking and fallback blocks must round-trip as returned.
                messages.append({"role": "assistant", "content": t.native})
            else:
                blocks = [{"type": "text", "text": t.text}] if t.text else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in t.tool_calls]
                messages.append({"role": "assistant", "content": blocks})
        else:
            # All results for one assistant turn go back in ONE user message.
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, **({"is_error": True} if r.is_error else {})}
                for r in t.tool_results
            ]})
    return messages


def render_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters, "strict": True} for t in tools]


def parse(response) -> ModelReply:
    content = response.content
    return ModelReply(
        text="".join(b.text for b in content if b.type == "text"),
        tool_calls=tuple(ToolCall(id=b.id, name=b.name, arguments=b.input) for b in content if b.type == "tool_use"),
        stop=_STOPS.get(response.stop_reason, "end"),
        raw_stop=str(response.stop_reason),
        model=getattr(response, "model", None) or settings.chat_model,
        provider=PROVIDER,
        native=content,
        usage={"input_tokens": getattr(response.usage, "input_tokens", None),
               "output_tokens": getattr(response.usage, "output_tokens", None)} if getattr(response, "usage", None) else {},
    )


class AnthropicModel:
    provider = PROVIDER

    def __init__(self, client=None):
        self.model = settings.chat_model
        self._client = client

    def unavailable_reason(self) -> str | None:
        if self._client is None and not settings.anthropic_api_key:
            return "Chat model is not configured; set ANTHROPIC_API_KEY in .env"
        return None

    @property
    def client(self):
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
        return self._client

    async def complete(self, *, system: str, turns: list[Turn], tools: list[ToolSpec]) -> ModelReply:
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                tools=render_tools(tools),
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=render(turns),
            )
        except anthropic.AuthenticationError:
            raise ModelUnavailable("Chat model rejected the API key; check ANTHROPIC_API_KEY in .env")
        except anthropic.RateLimitError:
            raise ModelUnavailable("Chat model is rate limited; try again shortly")
        except anthropic.APIConnectionError:
            raise ModelUnavailable("Could not reach the chat model")
        except anthropic.APIStatusError as e:
            raise ModelUnavailable(f"Chat model returned an error ({e.status_code})")
        return parse(response)
