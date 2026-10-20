"""SCIM 2.0 user provisioning (RFC 7643/7644), called by the tenant's IdP.

Okta creates users when they're assigned to Gatekeeper, updates them, and
deactivates them (PATCH active=false) when they're unassigned or fired.

Auth: a per-tenant bearer token, separate from user access tokens. The token
decides the tenant; every query then runs inside run_as_tenant(), so RLS keeps
one tenant's IdP from seeing or touching another tenant's users.
"""

import hashlib
import logging
import re
import uuid

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import audit, db

logger = logging.getLogger("gatekeeper.scim")
router = APIRouter(prefix="/scim/v2")

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
MEDIA_TYPE = "application/scim+json"

# SCIM-created users start as employees; their role comes from IdP groups at login.
DEFAULT_ROLE = "employee"
USER_COLUMNS = "id, external_id, email, given_name, family_name, active, created_at, updated_at"


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        self.status, self.detail, self.scim_type = status, detail, scim_type


def scim_response(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE)


def error_response(e: ScimError) -> JSONResponse:
    body = {"schemas": [ERROR_SCHEMA], "status": str(e.status), "detail": e.detail}
    if e.scim_type:
        body["scimType"] = e.scim_type
    headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
    return JSONResponse(body, status_code=e.status, media_type=MEDIA_TYPE, headers=headers)


async def scim_tenant(request: Request) -> str:
    """Dependency: resolve the tenant from the SCIM bearer token."""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else header  # Okta can send either form
    if not token:
        raise ScimError(401, "Missing SCIM bearer token")
    async with db.get_pool().acquire() as conn:
        tenant_id = await conn.fetchval(
            "SELECT id FROM tenants WHERE scim_token_hash = $1", hashlib.sha256(token.encode()).hexdigest()
        )
    if tenant_id is None:
        raise ScimError(401, "Invalid SCIM bearer token")
    return str(tenant_id)


async def json_body(request: Request) -> dict:
    # Okta sends application/scim+json, which FastAPI's body parsing doesn't treat as JSON.
    try:
        body = await request.json()
    except ValueError:
        raise ScimError(400, "Body must be JSON", "invalidSyntax")
    if not isinstance(body, dict):
        raise ScimError(400, "Body must be a JSON object", "invalidSyntax")
    return body


def to_scim(row: asyncpg.Record, request: Request) -> dict:
    user = {
        "schemas": [USER_SCHEMA],
        "id": str(row["id"]),
        "userName": row["email"],
        "name": {"givenName": row["given_name"] or "", "familyName": row["family_name"] or ""},
        "emails": [{"value": row["email"], "primary": True, "type": "work"}],
        "active": row["active"],
        "meta": {
            "resourceType": "User",
            "created": row["created_at"].isoformat(),
            "lastModified": row["updated_at"].isoformat(),
            "location": str(request.url_for("get_user", user_id=str(row["id"]))),
        },
    }
    if row["external_id"]:
        user["externalId"] = row["external_id"]
    return user


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ScimError(400, f"Expected a boolean, got {value!r}", "invalidValue")


def user_fields(body: dict) -> dict:
    """The SCIM attributes Gatekeeper stores. Anything else (e.g. password) is ignored, never stored."""
    fields = {}
    if "userName" in body:
        if not isinstance(body["userName"], str) or not body["userName"].strip():
            raise ScimError(400, "userName must be a non-empty string", "invalidValue")
        fields["email"] = body["userName"].strip().lower()
    if isinstance(body.get("name"), dict):
        if "givenName" in body["name"]:
            fields["given_name"] = body["name"]["givenName"]
        if "familyName" in body["name"]:
            fields["family_name"] = body["name"]["familyName"]
    if "active" in body:
        fields["active"] = parse_bool(body["active"])
    if isinstance(body.get("externalId"), str) and body["externalId"]:
        fields["external_id"] = body["externalId"]
    return fields


async def fetch_user(conn, user_id: str) -> asyncpg.Record:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise ScimError(404, "User not found")
    row = await conn.fetchrow(f"SELECT {USER_COLUMNS} FROM users WHERE id = $1", uid)
    if row is None:  # includes other tenants' users, which RLS hides
        raise ScimError(404, "User not found")
    return row


async def update_user(conn, tenant_id: str, user_id, fields: dict) -> asyncpg.Record:
    if not fields:
        return await fetch_user(conn, str(user_id))
    await audit.record(conn, tenant_id=tenant_id, user_id=str(user_id),
                       action="scim_deactivate" if fields.get("active") is False else "scim_update",
                       detail="changed: " + ", ".join(sorted(fields)))
    cols = list(fields)
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    try:
        return await conn.fetchrow(
            f"UPDATE users SET {sets}, updated_at = now() WHERE id = $1 RETURNING {USER_COLUMNS}",
            user_id, *[fields[c] for c in cols],
        )
    except asyncpg.UniqueViolationError:
        raise ScimError(409, "userName or externalId already in use", "uniqueness")


FILTER_RE = re.compile(r'^\s*userName\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


# ---------- routes ----------

@router.get("/Users")
async def list_users(request: Request, tenant_id: str = Depends(scim_tenant),
                     filter: str | None = None, startIndex: int = 1, count: int = 100):
    start, count = max(startIndex, 1), max(min(count, 200), 0)
    where, args = "", []
    if filter:
        m = FILTER_RE.match(filter)
        if not m:
            raise ScimError(400, 'Only filter=userName eq "..." is supported', "invalidFilter")
        where, args = "WHERE lower(email) = $1", [m.group(1).replace('\\"', '"').lower()]
    async with db.run_as_tenant(tenant_id) as conn:
        total = await conn.fetchval(f"SELECT count(*) FROM users {where}", *args)
        rows = await conn.fetch(
            f"SELECT {USER_COLUMNS} FROM users {where} ORDER BY created_at, id "
            f"OFFSET ${len(args) + 1} LIMIT ${len(args) + 2}", *args, start - 1, count,
        )
    return scim_response({
        "schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": start,
        "itemsPerPage": len(rows), "Resources": [to_scim(r, request) for r in rows],
    })


@router.get("/Users/{user_id}", name="get_user")
async def get_user(user_id: str, request: Request, tenant_id: str = Depends(scim_tenant)):
    async with db.run_as_tenant(tenant_id) as conn:
        return scim_response(to_scim(await fetch_user(conn, user_id), request))


@router.post("/Users")
async def create_user(request: Request, tenant_id: str = Depends(scim_tenant)):
    fields = user_fields(await json_body(request))
    if "email" not in fields:
        raise ScimError(400, "userName is required", "invalidValue")
    fields.setdefault("active", True)
    async with db.run_as_tenant(tenant_id) as conn:
        try:
            cols = list(fields)
            row = await conn.fetchrow(
                f"INSERT INTO users (tenant_id, role, {', '.join(cols)}) "
                f"VALUES ($1::uuid, $2, {', '.join(f'${i + 3}' for i in range(len(cols)))}) "
                f"RETURNING {USER_COLUMNS}",
                tenant_id, DEFAULT_ROLE, *[fields[c] for c in cols],
            )
        except asyncpg.UniqueViolationError:
            raise ScimError(409, "User already exists", "uniqueness")
        await audit.record(conn, tenant_id=tenant_id, user_id=str(row["id"]), action="scim_create",
                           detail=f"userName={row['email']}")
    logger.info("scim create: tenant=%s user=%s email=%s", tenant_id, row["id"], row["email"])
    return scim_response(to_scim(row, request), 201)


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request, tenant_id: str = Depends(scim_tenant)):
    fields = user_fields(await json_body(request))
    async with db.run_as_tenant(tenant_id) as conn:
        row = await fetch_user(conn, user_id)
        row = await update_user(conn, tenant_id, row["id"], fields)
    logger.info("scim replace: tenant=%s user=%s active=%s", tenant_id, row["id"], row["active"])
    return scim_response(to_scim(row, request))


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, tenant_id: str = Depends(scim_tenant)):
    body = await json_body(request)
    ops = body.get("Operations")
    if not isinstance(ops, list):
        raise ScimError(400, "PatchOp needs an Operations list", "invalidSyntax")
    fields: dict = {}
    for op in ops:
        if not isinstance(op, dict) or str(op.get("op", "")).lower() not in ("replace", "add"):
            raise ScimError(400, "Only add/replace operations are supported", "invalidSyntax")
        path, value = op.get("path"), op.get("value")
        if path is None and isinstance(value, dict):
            # {"op": "replace", "value": {"active": false}}  (Okta's usual form)
            fields.update(user_fields(value))
        elif path in ("active", "userName", "externalId"):
            fields.update(user_fields({path: value}))
        elif path in ("name.givenName", "name.familyName"):
            fields.update(user_fields({"name": {path.split(".")[1]: value}}))
        else:
            raise ScimError(400, f"Unsupported path {path!r}", "invalidPath")
    async with db.run_as_tenant(tenant_id) as conn:
        row = await fetch_user(conn, user_id)
        row = await update_user(conn, tenant_id, row["id"], fields)
    if fields.get("active") is False:
        logger.warning("scim deactivate: tenant=%s user=%s email=%s", tenant_id, row["id"], row["email"])
    return scim_response(to_scim(row, request))


@router.get("/Groups")
async def list_groups(tenant_id: str = Depends(scim_tenant), startIndex: int = 1):
    """Okta probes Groups during setup. Group push isn't supported (roles come from
    the IdP's login token), so report an empty list instead of a 404."""
    return scim_response({"schemas": [LIST_SCHEMA], "totalResults": 0, "startIndex": max(startIndex, 1),
                          "itemsPerPage": 0, "Resources": []})
