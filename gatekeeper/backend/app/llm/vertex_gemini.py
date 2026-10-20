"""Gemini on Vertex AI: the only model provider used inside the Enclave.

Reached through Google's restricted API range over Private Google Access, and
authenticated with Workload Identity (Application Default Credentials). There
is no API key.
"""

import asyncio
import itertools

from google import genai
from google.auth.exceptions import GoogleAuthError
from google.auth.exceptions import TransportError as AuthTransportError
from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.llm.base import ModelReply, ModelUnavailable, ToolCall, ToolSpec, Turn

PROVIDER = "vertex-gemini"

_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "RECITATION"}

_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError, asyncio.TimeoutError)
for _mod in ("httpx2", "httpx", "aiohttp"):
    try:
        _m = __import__(_mod)
        _TRANSPORT_ERRORS += ((_m.ClientError,) if _mod == "aiohttp" else (_m.TransportError,))
    except ImportError:
        pass

_ids = itertools.count(1)


def render(turns: list[Turn]) -> list[types.Content]:
    contents = []
    for t in turns:
        if t.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=t.text)]))
        elif t.role == "assistant":
            if t.provider == PROVIDER and t.native is not None:
                # Replay unchanged: Gemini's thought signatures must come back with the function calls.
                contents.append(t.native)
            else:
                parts = [types.Part.from_text(text=t.text)] if t.text else []
                parts += [types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.arguments))
                          for c in t.tool_calls]
                contents.append(types.Content(role="model", parts=parts))
        else:
            # All results for one model turn go back together, matched by id and name.
            contents.append(types.Content(role="user", parts=[
                types.Part(function_response=types.FunctionResponse(
                    id=r.call_id, name=r.name,
                    response={"error": r.content} if r.is_error else {"result": r.content},
                ))
                for r in t.tool_results
            ]))
    return contents


def config(system: str, tools: list[ToolSpec]) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools
        ])],
        # The agent runs every tool itself (as the signed-in user); the SDK must never call Python functions.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        max_output_tokens=8192,
    )


def parse(response: types.GenerateContentResponse, model: str) -> ModelReply:
    usage = response.usage_metadata
    usage = {"input_tokens": usage.prompt_token_count, "output_tokens": usage.candidates_token_count} if usage else {}
    if not response.candidates:
        # The prompt itself was blocked (prompt_feedback.block_reason).
        reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        return ModelReply(text="", tool_calls=(), stop="refusal", raw_stop=f"PROMPT_BLOCKED:{reason}",
                          model=model, provider=PROVIDER, usage=usage)
    candidate = response.candidates[0]
    content = candidate.content or types.Content(role="model", parts=[])
    parts = content.parts or []
    calls = tuple(
        ToolCall(id=p.function_call.id or f"call-{next(_ids)}", name=p.function_call.name, arguments=p.function_call.args)
        for p in parts if p.function_call
    )
    finish = candidate.finish_reason.name if candidate.finish_reason else "FINISH_REASON_UNSPECIFIED"
    if finish in _REFUSALS:
        stop = "refusal"
    elif calls:
        stop = "tool_use"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    else:
        stop = "end"  # includes MALFORMED_FUNCTION_CALL: no usable call, the agent answers "no access"
    return ModelReply(
        text="".join(p.text for p in parts if p.text and not p.thought),
        tool_calls=calls, stop=stop, raw_stop=finish, model=model, provider=PROVIDER,
        native=content, usage=usage,
    )


class VertexGeminiModel:
    provider = PROVIDER

    def __init__(self, client=None):
        self.model = settings.gemini_model
        self._client = client

    def unavailable_reason(self) -> str | None:
        if self._client is None and not settings.vertex_project:
            return "Chat model is not configured; set VERTEX_PROJECT (and VERTEX_LOCATION) in .env"
        return None

    @property
    def client(self):
        if self._client is None:
            self._client = genai.Client(
                vertexai=True, project=settings.vertex_project, location=settings.vertex_location,
                http_options=types.HttpOptions(timeout=settings.vertex_timeout_seconds * 1000),
            )
        return self._client

    async def complete(self, *, system: str, turns: list[Turn], tools: list[ToolSpec]) -> ModelReply:
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model, contents=render(turns), config=config(system, tools),
            )
        except genai_errors.APIError as e:
            raise ModelUnavailable(f"Vertex AI returned an error ({e.code})")
        except AuthTransportError:
            # Fetching an access token needs Google's token endpoint; blocked egress lands here first.
            raise ModelUnavailable("Could not reach Vertex AI (Google's token endpoint is unreachable)")
        except GoogleAuthError:
            raise ModelUnavailable("Could not authenticate to Vertex AI (Workload Identity / ADC)")
        except _TRANSPORT_ERRORS:
            raise ModelUnavailable("Could not reach Vertex AI")
        return parse(response, self.model)
