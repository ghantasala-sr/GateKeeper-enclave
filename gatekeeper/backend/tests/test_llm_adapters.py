"""Contract tests for the two model adapters, with recorded responses and no network.

Each adapter must: parse a reply into neutral types, replay its own previous
turn unchanged (Gemini thought signatures, Claude thinking blocks), send tool
results back matched by id, map refusals, and turn every "can't reach the
model" failure into ModelUnavailable (which /chat answers with a clean 503).
"""

import asyncio
from types import SimpleNamespace

import anthropic
import google.auth.exceptions
import httpx
import pytest
from anthropic.types.beta import BetaMessage
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.oauth2.credentials import Credentials

from app.llm import ModelUnavailable, ToolResult, ToolSpec, Turn
from app.llm import anthropic as claude
from app.llm import vertex_gemini as gemini
from app.rag.agent import SEARCH_TOOL, SYSTEM_PROMPT

UNREACHABLE = "http://127.0.0.1:9"  # nothing listens on the discard port


class Recorder:
    """Stands in for an SDK method: records the call, then returns or raises."""

    def __init__(self, result):
        self.result, self.calls = result, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def run(coro):
    return asyncio.run(coro)


# ---------- Gemini on Vertex AI ----------

def gemini_response(parts, finish="STOP", **extra):
    return types.GenerateContentResponse.model_validate({
        "candidates": [{"content": {"role": "model", "parts": parts}, "finish_reason": finish}],
        "usage_metadata": {"prompt_token_count": 120, "candidates_token_count": 9},
        **extra,
    })


TOOL_CALL = gemini_response([
    {"text": "I should search.", "thought": True},
    {"function_call": {"id": "fc-1", "name": "search_documents", "args": {"query": "PTO policy"}},
     "thought_signature": "c2lnbmF0dXJl"},
])


def fake_gemini(result):
    create = Recorder(result)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=create)))
    return gemini.VertexGeminiModel(client=client), create


def test_gemini_parses_a_tool_call():
    reply = gemini.parse(TOOL_CALL, "gemini-3.8-flash")
    assert reply.stop == "tool_use" and reply.raw_stop == "STOP"
    [call] = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("fc-1", "search_documents", {"query": "PTO policy"})
    assert reply.text == ""  # thoughts are never shown as the answer
    assert reply.usage == {"input_tokens": 120, "output_tokens": 9}


def test_gemini_replays_its_turn_unchanged_and_returns_results_by_id():
    reply = gemini.parse(TOOL_CALL, "gemini-3.8-flash")
    results = Turn(role="tool", tool_results=(
        ToolResult(call_id="fc-1", name="search_documents", content="<chunk>…</chunk>"),
        ToolResult(call_id="fc-2", name="search_documents", content="Invalid arguments", is_error=True),
    ))
    contents = gemini.render([Turn(role="user", text="PTO?"), reply.as_turn(), results])

    assert contents[0].role == "user" and contents[0].parts[0].text == "PTO?"
    assert contents[1] is reply.native                      # signature-carrying turn, untouched
    assert contents[1].parts[1].thought_signature == b"signature"
    ok, bad = (p.function_response for p in contents[2].parts)
    assert (ok.id, ok.name, ok.response) == ("fc-1", "search_documents", {"result": "<chunk>…</chunk>"})
    assert (bad.id, bad.response) == ("fc-2", {"error": "Invalid arguments"})


def test_gemini_request_config():
    model, create = fake_gemini(gemini_response([{"text": "20 days [ACME-PUB-02]."}]))
    reply = run(model.complete(system=SYSTEM_PROMPT, turns=[Turn(role="user", text="PTO?")], tools=[SEARCH_TOOL]))
    assert reply.stop == "end" and reply.text == "20 days [ACME-PUB-02]."

    [call] = create.calls
    cfg = call["config"]
    assert call["model"] == "gemini-3.8-flash"
    assert cfg.system_instruction == SYSTEM_PROMPT
    assert cfg.automatic_function_calling.disable is True  # the SDK must never run tools itself
    [decl] = cfg.tools[0].function_declarations
    assert decl.name == "search_documents" and decl.parameters_json_schema == SEARCH_TOOL.parameters
    assert set(decl.parameters_json_schema["properties"]) == {"query"}


@pytest.mark.parametrize("finish, stop", [
    ("SAFETY", "refusal"), ("PROHIBITED_CONTENT", "refusal"), ("BLOCKLIST", "refusal"), ("SPII", "refusal"),
    ("MAX_TOKENS", "max_tokens"), ("MALFORMED_FUNCTION_CALL", "end"), ("STOP", "end"),
])
def test_gemini_finish_reasons(finish, stop):
    assert gemini.parse(gemini_response([{"text": "x"}], finish=finish), "m").stop == stop


def test_gemini_blocked_prompt_is_a_refusal():
    blocked = types.GenerateContentResponse.model_validate({"prompt_feedback": {"block_reason": "SAFETY"}})
    reply = gemini.parse(blocked, "m")
    assert reply.stop == "refusal" and reply.tool_calls == () and "SAFETY" in reply.raw_stop


def test_gemini_call_without_id_gets_one():
    reply = gemini.parse(gemini_response([{"function_call": {"name": "search_documents", "args": {"query": "q"}}}]), "m")
    assert reply.tool_calls[0].id


@pytest.mark.parametrize("error", [
    genai_errors.APIError(403, {"error": {"code": 403, "message": "Permission denied", "status": "PERMISSION_DENIED"}}),
    genai_errors.APIError(429, {"error": {"code": 429, "message": "Quota", "status": "RESOURCE_EXHAUSTED"}}),
    httpx.ConnectError("connection refused"),
    google.auth.exceptions.TransportError("token endpoint unreachable"),
    google.auth.exceptions.DefaultCredentialsError("no ADC"),
    httpx.ReadTimeout("timed out"),
    TimeoutError(),
])
def test_gemini_failures_become_model_unavailable(error):
    model, _ = fake_gemini(error)
    with pytest.raises(ModelUnavailable):
        run(model.complete(system="s", turns=[Turn(role="user", text="hi")], tools=[SEARCH_TOOL]))


def test_gemini_unreachable_with_the_real_sdk():
    """What the Enclave sees if the restricted Google API route is missing: the real client, no route."""
    client = genai.Client(vertexai=True, project="enclave-test", location="global", credentials=Credentials("unused"),
                          http_options=types.HttpOptions(base_url=UNREACHABLE, timeout=3000))
    with pytest.raises(ModelUnavailable, match="reach Vertex AI"):
        run(gemini.VertexGeminiModel(client=client).complete(
            system="s", turns=[Turn(role="user", text="hi")], tools=[SEARCH_TOOL]))


def test_gemini_needs_a_project(monkeypatch):
    monkeypatch.setattr(gemini.settings, "vertex_project", "")
    assert "VERTEX_PROJECT" in gemini.VertexGeminiModel().unavailable_reason()
    monkeypatch.setattr(gemini.settings, "vertex_project", "my-project")
    assert gemini.VertexGeminiModel().unavailable_reason() is None


# ---------- Claude (local development) ----------

CLAUDE_TOOL_CALL = BetaMessage.model_validate({
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "Search first.", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "search_documents", "input": {"query": "PTO policy"}},
    ],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 300, "output_tokens": 40},
})


def fake_claude(result):
    create = Recorder(result)
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    return claude.AnthropicModel(client=client), create


def test_claude_parses_and_replays_unchanged():
    reply = claude.parse(CLAUDE_TOOL_CALL)
    assert reply.stop == "tool_use" and reply.usage == {"input_tokens": 300, "output_tokens": 40}
    [call] = reply.tool_calls
    assert (call.id, call.arguments) == ("toolu_1", {"query": "PTO policy"})

    results = Turn(role="tool", tool_results=(
        ToolResult(call_id="toolu_1", name="search_documents", content="<chunk>…</chunk>"),
        ToolResult(call_id="toolu_2", name="search_documents", content="Invalid arguments", is_error=True),
    ))
    messages = claude.render([Turn(role="user", text="PTO?"), reply.as_turn(), results])
    assert messages[1] == {"role": "assistant", "content": CLAUDE_TOOL_CALL.content}  # thinking block kept
    assert messages[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "<chunk>…</chunk>"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "Invalid arguments", "is_error": True},
    ]}


def test_claude_request():
    end = {**CLAUDE_TOOL_CALL.model_dump(), "stop_reason": "end_turn", "content": [{"type": "text", "text": "20 days."}]}
    model, create = fake_claude(BetaMessage.model_validate(end))
    reply = run(model.complete(system=SYSTEM_PROMPT, turns=[Turn(role="user", text="PTO?")], tools=[SEARCH_TOOL]))
    assert reply.stop == "end" and reply.text == "20 days."
    [call] = create.calls
    assert call["system"] == SYSTEM_PROMPT and call["tools"][0]["input_schema"] == SEARCH_TOOL.parameters
    assert call["tools"][0]["strict"] is True


def test_claude_refusal():
    refused = BetaMessage.model_validate({**CLAUDE_TOOL_CALL.model_dump(), "content": [], "stop_reason": "refusal"})
    assert claude.parse(refused).stop == "refusal"


def test_claude_unreachable_with_the_real_sdk():
    client = anthropic.AsyncAnthropic(api_key="unused", base_url=UNREACHABLE, max_retries=0, timeout=3)
    with pytest.raises(ModelUnavailable, match="reach the chat model"):
        run(claude.AnthropicModel(client=client).complete(
            system="s", turns=[Turn(role="user", text="hi")], tools=[SEARCH_TOOL]))


def test_claude_needs_a_key(monkeypatch):
    monkeypatch.setattr(claude.settings, "anthropic_api_key", "")
    assert "ANTHROPIC_API_KEY" in claude.AnthropicModel().unavailable_reason()


# ---------- choosing a provider ----------

@pytest.mark.parametrize("provider, cls", [("vertex-gemini", gemini.VertexGeminiModel), ("anthropic", claude.AnthropicModel)])
def test_get_model_follows_llm_provider(monkeypatch, provider, cls):
    from app import llm
    monkeypatch.setattr(llm.settings, "llm_provider", provider)
    llm.get_model.cache_clear()
    try:
        assert isinstance(llm.get_model(), cls)
    finally:
        llm.get_model.cache_clear()


def test_tool_spec_is_provider_neutral():
    assert isinstance(SEARCH_TOOL, ToolSpec) and SEARCH_TOOL.parameters["additionalProperties"] is False
