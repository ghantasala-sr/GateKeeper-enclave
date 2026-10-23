"""Prometheus metrics, served on their own port (METRICS_PORT, default 9090).

A separate port, not a route on the API: the Gateway only forwards :8000, so the
metrics are readable inside the cluster (by Managed Service for Prometheus) and
nowhere else.
"""

import time

from prometheus_client import Counter, Histogram, start_http_server
from starlette.types import ASGIApp, Receive, Scope, Send

REQUESTS = Counter("gatekeeper_http_requests_total", "HTTP requests handled.", ["route", "method", "status"])
LATENCY = Histogram("gatekeeper_http_request_duration_seconds", "Time to handle a request.", ["route", "method"],
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 80))
CHUNKS_RETRIEVED = Counter("gatekeeper_chat_chunks_retrieved_total", "Chunks the agent's searches returned.")
FORBIDDEN_RETRIEVED = Counter("gatekeeper_forbidden_retrieved_total",
                              "Retrieved chunks the independent check says the user may not see. Must stay 0.")
TOOL_CALLS_REJECTED = Counter("gatekeeper_tool_calls_rejected_total",
                              "Tool calls refused (identity-stuffed arguments, unknown tools).")
MODEL_UNAVAILABLE = Counter("gatekeeper_model_unavailable_total", "Chats answered 503 because the model was unreachable.")


class MetricsMiddleware:
    """Counts and times every request by its route TEMPLATE (/scim/v2/Users/{id}), never the raw path."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            REQUESTS.labels(route, scope["method"], str(status["code"])).inc()
            LATENCY.labels(route, scope["method"]).observe(time.perf_counter() - started)


def serve(port: int) -> None:
    if port:
        start_http_server(port)
