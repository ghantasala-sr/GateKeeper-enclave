"""Metrics: counted by route template, and exposed only on the separate metrics port."""

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import main, metrics


def value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def test_requests_are_counted_by_route_template_not_raw_path():
    with TestClient(main.app) as client:
        before = value("gatekeeper_http_requests_total", route="/health", method="GET", status="200")
        client.get("/health")
        assert value("gatekeeper_http_requests_total", route="/health", method="GET", status="200") == before + 1

        # Unknown paths share one label, so a scanner can't blow up the metric's cardinality.
        before = value("gatekeeper_http_requests_total", route="unmatched", method="GET", status="404")
        client.get("/no/such/path/12345")
        client.get("/another/random/path")
        assert value("gatekeeper_http_requests_total", route="unmatched", method="GET", status="404") == before + 2


def test_denied_requests_are_visible():
    with TestClient(main.app) as client:
        before = value("gatekeeper_http_requests_total", route="/me", method="GET", status="401")
        client.get("/me")                               # no token
        client.get("/me", headers={"Authorization": "Bearer forged"})
        assert value("gatekeeper_http_requests_total", route="/me", method="GET", status="401") == before + 2


def test_latency_is_recorded():
    with TestClient(main.app) as client:
        before = value("gatekeeper_http_request_duration_seconds_count", route="/health", method="GET")
        client.get("/health")
        assert value("gatekeeper_http_request_duration_seconds_count", route="/health", method="GET") == before + 1


def test_metrics_are_not_an_api_route():
    """The Gateway forwards the API port; metrics must not be reachable through it."""
    with TestClient(main.app) as client:
        assert client.get("/metrics").status_code == 404


def test_rejected_tool_calls_are_counted():
    from app.rag import agent
    from tests.test_agent import FakeChatModel, principal, tool_use, with_db
    assert hasattr(metrics, "TOOL_CALLS_REJECTED")
    before = value("gatekeeper_tool_calls_rejected_total")
    # A call stuffed with an identity is refused by the tool's argument check.
    fake = FakeChatModel(([tool_use("t1", {"query": "x", "tenant_id": "someone"})], "tool_use"), (["no"], "end"))

    async def test(tenants):
        agent_get_model = agent.get_model
        agent.get_model = lambda: fake
        try:
            await agent.run_agent(principal(tenants), "q")
        finally:
            agent.get_model = agent_get_model
    with_db(test)
    assert value("gatekeeper_tool_calls_rejected_total") == before + 1
