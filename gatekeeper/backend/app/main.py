import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import audit, db, metrics
from app import trace as trace_log
from app.auth import oidc, saml, tokens
from app.auth.bridge import Principal
from app.scim import routes as scim
from app.config import settings
from app.llm import ModelUnavailable, get_model
from app.rag.embeddings import embed
from app.rag.agent import run_agent

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    embed(["warm up"])  # load the embedding model once at startup
    metrics.serve(settings.metrics_port)
    yield
    await db.close_pool()


app = FastAPI(title="Gatekeeper", lifespan=lifespan)
# Holds only the in-progress login (state, nonce, PKCE verifier), so keep it short-lived.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="gk_login",
    max_age=600,
    same_site="lax",
    https_only=settings.okta_redirect_uri.startswith("https://"),
    # Lax still sends this cookie on the IdP's POST to /auth/saml/acs because Keycloak
    # (localhost:8080) is the same site as us. A real Entra IdP is cross-site: that
    # needs same_site="none" over HTTPS, or the InResponseTo check can't find the request.
)
# The frontend (a different origin) calls the API with a bearer token. No cookies
# cross origins (allow_credentials=False), so CSRF isn't a concern for the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url.rstrip("/")],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)
# Outermost, so it times everything, including requests the other middleware answer.
app.add_middleware(metrics.MetricsMiddleware)
app.include_router(oidc.router)
app.include_router(saml.router)
app.include_router(scim.router)
app.add_exception_handler(scim.ScimError, lambda request, e: scim.error_response(e))


class ChatRequest(BaseModel):
    question: str


class ChunkOut(BaseModel):
    reference: str
    tenant: str
    classification: str
    allowed: bool


class ChatResponse(BaseModel):
    answer: str
    chunks: list[ChunkOut]
    forbidden_retrieved: int
    trace: dict | None = None  # what the backend did, for the frontend's inspector


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Deliberately checks nothing else, so a database
    outage makes pods stop taking traffic (/ready) instead of restarting them all."""
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """Readiness: can this pod serve requests right now? Needs a working database connection."""
    try:
        async with asyncio.timeout(2):
            async with db.get_pool().acquire() as conn:
                await conn.fetchval("SELECT 1")
    except Exception as e:  # any failure means "don't send me traffic", never a crash
        logging.getLogger("gatekeeper").warning("not ready: %r", e)
        return JSONResponse({"status": "database unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/.well-known/jwks.json")
def jwks() -> dict:
    return tokens.jwks()


@app.get("/me")
async def me(principal: Principal = Depends(tokens.require_scope("docs:read"))) -> dict:
    """Who the token belongs to, for the frontend's company/role badge."""
    async with db.run_as(principal) as conn:
        tenant = await conn.fetchrow("SELECT name, idp_type FROM tenants WHERE id = $1::uuid", principal.tenant_id)
        last_login = await conn.fetchval("SELECT last_login_at FROM users WHERE id = $1::uuid", principal.user_id)
    return {"email": principal.email, "role": principal.role, "scopes": list(principal.scopes),
            "tenant_id": principal.tenant_id, "tenant": tenant["name"], "idp_type": tenant["idp_type"],
            "last_login_at": last_login.isoformat() if last_login else None}


@app.post("/chat")
async def chat(req: ChatRequest, request: Request,
               principal: Principal = Depends(tokens.require_scope("docs:read"))) -> ChatResponse:
    trace = request.state.trace
    # Who is asking now comes only from the verified access token.
    with trace.step("Row-level security context set", "rls") as t:
        async with db.run_as(principal) as conn:
            tenant_name = await conn.fetchval("SELECT name FROM tenants WHERE id = $1::uuid", principal.tenant_id)
            visible = await conn.fetch(
                "SELECT classification, count(*) AS n FROM documents GROUP BY 1 ORDER BY 1")
            other_tenants = await conn.fetchval("SELECT count(*) FROM documents WHERE tenant_id <> $1::uuid",
                                                principal.tenant_id)
        t["details"] = {
            "set_config": {"app.tenant_id": f"{principal.tenant_id} ({tenant_name})", "app.role": principal.role},
            "scope": "SET LOCAL: this transaction only, never leaks to the next request on a pooled connection",
            "db_role": "gatekeeper_app (not owner, not superuser, no BYPASSRLS)",
            "documents_this_connection_can_see": {r["classification"]: r["n"] for r in visible},
            "other_tenants_documents_visible": other_tenants,
            "policies": ["tenant_isolation: tenant_id = app.tenant_id",
                         "role_access (RESTRICTIVE): classification allowed for app.role"],
        }
    user = {"email": principal.email, "role": principal.role, "tenant": tenant_name}

    if reason := get_model().unavailable_reason():
        raise HTTPException(503, reason)
    try:
        # The agent searches as this principal; RLS limits what its tool can see.
        text, chunks = await run_agent(principal, req.question, trace=trace)
    except ModelUnavailable as e:
        metrics.MODEL_UNAVAILABLE.inc()
        raise HTTPException(503, str(e))
    metrics.CHUNKS_RETRIEVED.inc(len(chunks))

    chunk_ids = list(dict.fromkeys(c.id for c in chunks))
    with trace.step("Audit log entry written", "audit") as t:
        async with db.run_as(principal) as conn:
            await audit.record(conn, tenant_id=principal.tenant_id, user_id=principal.user_id, action="chat",
                               question=req.question, chunk_ids=chunk_ids)
        t["details"] = {"table": "audit_log", "action": "chat", "chunk_ids_recorded": len(chunk_ids),
                        "app_privileges": "INSERT only: it can't read, change, or delete the log"}
    with trace.step("Independent leak check", "check") as t:
        logged = trace_log.record(user, req.question, chunks, text)
        metrics.FORBIDDEN_RETRIEVED.inc(logged["forbidden_retrieved"])
        t["ok"] = logged["forbidden_retrieved"] == 0
        t["details"] = {"method": "re-checks every retrieved chunk in Python, separately from RLS",
                        "chunks_checked": len(chunks), "forbidden_retrieved": logged["forbidden_retrieved"]}
    return ChatResponse(
        answer=text,
        chunks=[ChunkOut(**{k: r[k] for k in ("reference", "tenant", "classification", "allowed")}) for r in logged["retrieved"]],
        forbidden_retrieved=logged["forbidden_retrieved"],
        trace=trace.to_dict(),
    )


@app.get("/admin/stats")
async def admin_stats(principal: Principal = Depends(tokens.require_scope("docs:admin"))) -> dict:
    """Test-only route to prove scope checks. Counts the admin's own tenant only."""
    async with db.run_as(principal) as conn:
        # RLS scopes these counts; the explicit tenant filter is a second layer, not the guard.
        row = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM documents WHERE tenant_id = $1::uuid) AS documents,
                   (SELECT count(*) FROM chunks    WHERE tenant_id = $1::uuid) AS chunks,
                   (SELECT count(*) FROM users     WHERE tenant_id = $1::uuid) AS users
            """,
            principal.tenant_id,
        )
    return {"tenant_id": principal.tenant_id, **dict(row)}
