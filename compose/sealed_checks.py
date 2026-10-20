"""Checks run inside the sealed network, from the backend container. Exit 1 if any fails.

1. Nothing gets out: the internet, Google APIs and Anthropic are all unreachable.
2. Everything inside is reachable: the frontend, the API, Keycloak.
3. All six test users sign in (Acme over OIDC, Globex over SAML) with the right role.
4. Chat fails cleanly: a 503 that says Vertex AI is unreachable, quickly, not a hang.
"""

import os
import socket
import sys
import time

import httpx

sys.path.insert(0, "/checks")
from check_logins import USERS, claims, login  # noqa: E402

API = "http://api.enclave.internal:8000"
OUTSIDE = [
    "https://www.google.com",
    "https://aiplatform.googleapis.com",
    "https://oauth2.googleapis.com/token",
    "https://api.anthropic.com",
    "https://huggingface.co",
]
INSIDE = [
    "http://gatekeeper.enclave.internal:3000/",
    f"{API}/health",
    "http://auth.enclave.internal:8080/realms/acme/.well-known/openid-configuration",
]
CHAT_DEADLINE_SECONDS = 30

failures = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  ({detail})' if detail else ''}")


def unreachable(url: str) -> tuple[bool, str]:
    try:
        r = httpx.get(url, timeout=5)
        return False, f"got HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return True, type(e).__name__


def raw_ip_blocked() -> tuple[bool, str]:
    """Not just DNS: a raw TCP connection to a public IP must fail too."""
    try:
        socket.create_connection(("8.8.8.8", 443), timeout=5).close()
        return False, "connected to 8.8.8.8:443"
    except OSError as e:
        return True, type(e).__name__


print("-- egress (must all fail)")
for url in OUTSIDE:
    ok, detail = unreachable(url)
    check(ok, f"no route to {url}", detail)
ok, detail = raw_ip_blocked()
check(ok, "no route to 8.8.8.8:443 (raw TCP)", detail)

print("-- inside the network (must all work)")
for url in INSIDE:
    try:
        status = httpx.get(url, timeout=10).status_code
        check(status == 200, f"reached {url}", f"HTTP {status}")
    except httpx.HTTPError as e:
        check(False, f"reached {url}", repr(e))

print("-- logins through Keycloak")
tokens = {}
for kind, email, role, password_var in USERS:
    try:
        tokens[email] = login(API, kind, email, os.environ[password_var])["access_token"]
        got = claims(tokens[email]).get("role")
        check(got == role, f"{kind} login {email}", f"role={got}")
    except Exception as e:
        check(False, f"{kind} login {email}", str(e)[:200])

print("-- chat with Vertex AI unreachable")
token = tokens.get("employee@acme.example")
if token:
    started = time.monotonic()
    r = httpx.post(f"{API}/chat", json={"question": "How much PTO do I get?"},
                   headers={"Authorization": f"Bearer {token}"}, timeout=CHAT_DEADLINE_SECONDS + 30)
    took = time.monotonic() - started
    detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else r.text[:100]
    check(r.status_code == 503 and "reach Vertex AI" in detail, "chat returns 503", f"{r.status_code} {detail!r}")
    check(took < CHAT_DEADLINE_SECONDS, f"chat fails fast (< {CHAT_DEADLINE_SECONDS}s)", f"{took:.1f}s")
else:
    check(False, "chat returns 503", "no Acme employee token to test with")

print(f"\n{'all checks passed' if not failures else f'{failures} check(s) FAILED'}")
sys.exit(1 if failures else 0)
