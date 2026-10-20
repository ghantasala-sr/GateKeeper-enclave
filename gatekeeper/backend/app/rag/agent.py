"""A LangGraph agent that searches documents with exactly the user's permissions.

The graph: call_model -> (tool_use?) -> run_tools -> call_model -> ... -> END.

The Principal lives in the graph state, put there by run_agent() from the
verified access token. The model never sees it and can't change it: the only
thing it controls is the `query` string. The tool reads the Principal from
state and searches inside db.run_as(principal), so RLS applies exactly as if
the user had searched directly. There is no "master key" connection.
"""

import operator
import time
from typing import Annotated, Any, TypedDict

import anthropic
from langgraph.graph import END, START, StateGraph

from app import db
from app.auth.bridge import Principal
from app.config import settings
from app.rag.search import Chunk, search
from app.tracing import RequestTrace

MAX_TOOL_ROUNDS = 3

SYSTEM_PROMPT = """You answer employees' questions about internal company documents.

Use the search_documents tool to find relevant documents before answering. Only
answer from what the tool returns. If it doesn't contain the answer, reply
exactly: "I don't have access to that information." Keep answers short and cite
the document reference in brackets, e.g. [ACME-PUB-02]."""

SEARCH_TOOL = {
    "name": "search_documents",
    "description": "Search the internal documents this employee can access. Returns the most relevant passages.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for, in plain words."}},
        "required": ["query"],
        "additionalProperties": False,
    },
}

NO_ACCESS = "I don't have access to that information."

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)  # key checked in /chat


class AgentState(TypedDict):
    principal: Principal                                 # set once by run_agent(); nodes only read it
    trace: RequestTrace | None                           # optional; for the frontend's inspector
    messages: Annotated[list[dict[str, Any]], operator.add]  # append-only conversation with Claude
    retrieved: Annotated[list[Chunk], operator.add]      # every chunk any tool call returned
    stop_reason: str
    answer: str
    tool_rounds: int


async def search_documents(principal: Principal, tool_input: Any) -> tuple[list[Chunk], str | None]:
    """The tool. Returns (chunks, error). Identity comes from `principal`, never from tool_input."""
    # Reject anything but exactly {"query": "<text>"}. A model (or an injected prompt)
    # passing tenant_id/role/user_id gets an error, not a silently-ignored extra.
    if not isinstance(tool_input, dict) or set(tool_input) != {"query"} or not isinstance(tool_input["query"], str):
        return [], "Invalid arguments: search_documents accepts only {\"query\": string}."
    async with db.run_as(principal) as conn:
        return await search(conn, tool_input["query"]), None


def _short(tool_input: Any) -> str:
    q = tool_input.get("query") if isinstance(tool_input, dict) else None
    text = repr(q) if isinstance(q, str) else str(tool_input)
    return text if len(text) <= 48 else text[:45] + "…'"


def format_results(chunks: list[Chunk]) -> str:
    if not chunks:
        return "No matching documents."
    return "\n\n".join(f'<chunk reference="{c.reference}">\n{c.content}\n</chunk>' for c in chunks)


def _trace_step(state: AgentState, name: str, layer: str):
    trace = state.get("trace")
    return trace.step(name, layer) if trace else _NullStep()


class _NullStep:
    def __enter__(self):
        return {"details": {}}

    def __exit__(self, *exc):
        return False


async def call_model(state: AgentState) -> dict:
    n = sum(1 for m in state["messages"] if m["role"] == "assistant") + 1
    with _trace_step(state, f"Model call {n}", "model") as t:
        response = await _create(state)
        usage = getattr(response, "usage", None)
        t["details"] = {
            "model": settings.chat_model,
            "stop_reason": response.stop_reason,
            "tool_calls_requested": [{"tool": b.name, "input": b.input} for b in response.content if b.type == "tool_use"],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "sees": "only the question and chunks returned by earlier tool calls; never the user's identity",
        }
    return _model_update(response)


async def _create(state: AgentState):
    return await client.beta.messages.create(
        model=settings.chat_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[SEARCH_TOOL],
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=state["messages"],
    )


def _model_update(response) -> dict:
    update: dict = {
        # Echo content back unchanged (thinking and fallback blocks included).
        "messages": [{"role": "assistant", "content": response.content}],
        "stop_reason": response.stop_reason,
    }
    if response.stop_reason == "refusal":
        update["answer"] = "Sorry, I can't help with that request."
    elif response.stop_reason != "tool_use":
        update["answer"] = "".join(b.text for b in response.content if b.type == "text") or NO_ACCESS
    return update


async def run_tools(state: AgentState) -> dict:
    principal = state["principal"]
    tool_uses = [b for b in state["messages"][-1]["content"] if b.type == "tool_use"]
    results, retrieved = [], []
    for block in tool_uses:
        with _trace_step(state, f"{block.name}({_short(block.input)})", "tool") as t:
            if block.name != SEARCH_TOOL["name"]:
                chunks, error = [], f"Unknown tool {block.name!r}."
            else:
                chunks, error = await search_documents(principal, block.input)
            t["ok"] = error is None
            t["details"] = {
                "model_asked_for": block.input,
                "accepted": error is None,
                **({"rejected_because": error} if error else {}),
                "ran_as": f"{principal.role} @ tenant {principal.tenant_id[:8]}… (db.run_as, from the token, not the model)",
                "rls": "tenant_isolation AND role_access policies filtered rows inside Postgres",
                "returned": [{"ref": c.reference, "classification": c.classification, "distance": round(c.distance, 3)}
                             for c in chunks],
            }
        retrieved.extend(chunks)
        result = {"type": "tool_result", "tool_use_id": block.id, "content": error or format_results(chunks)}
        if error:
            result["is_error"] = True
        results.append(result)
    # All results go back in ONE user message.
    return {"messages": [{"role": "user", "content": results}], "retrieved": retrieved,
            "tool_rounds": state["tool_rounds"] + 1}


def after_model(state: AgentState) -> str:
    if state["stop_reason"] == "tool_use" and state["tool_rounds"] < MAX_TOOL_ROUNDS:
        return "run_tools"
    return END


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("run_tools", run_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", after_model, ["run_tools", END])
    graph.add_edge("run_tools", "call_model")
    return graph.compile()


agent = build_graph()


async def run_agent(principal: Principal, question: str, trace: RequestTrace | None = None) -> tuple[str, list[Chunk]]:
    """Answer `question` as `principal`. Returns (answer, every chunk the tools retrieved)."""
    if not isinstance(principal, Principal):
        raise TypeError("run_agent needs a verified Principal")
    final = await agent.ainvoke({
        "principal": principal,
        "trace": trace,
        "messages": [{"role": "user", "content": question}],
        "retrieved": [],
        "stop_reason": "",
        "answer": "",
        "tool_rounds": 0,
    })
    # Ran out of tool rounds without a final answer.
    return final["answer"] or NO_ACCESS, final["retrieved"]
