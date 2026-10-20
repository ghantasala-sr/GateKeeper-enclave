"""Sign in all six test users through Keycloak, the way a browser does, and check each role.

Acme logs in with OIDC (authorization code + PKCE), Globex with SAML (signed
POST to the ACS). Every hop goes through the real backend, so this checks the
realm files, the issuer pinning, the groups/role mapping and the login cookie.

    python keycloak/check_logins.py --api http://localhost:8000
Passwords come from ACME_TEST_PASSWORD and GLOBEX_TEST_PASSWORD.
"""

import argparse
import base64
import html
import json
import os
import re
import sys

import httpx

USERS = [
    ("okta", "employee@acme.example", "employee", "ACME_TEST_PASSWORD"),
    ("okta", "hr.manager@acme.example", "hr_manager", "ACME_TEST_PASSWORD"),
    ("okta", "admin@acme.example", "admin", "ACME_TEST_PASSWORD"),
    ("saml", "employee@globex.example", "employee", "GLOBEX_TEST_PASSWORD"),
    ("saml", "hr.manager@globex.example", "hr_manager", "GLOBEX_TEST_PASSWORD"),
    ("saml", "admin@globex.example", "admin", "GLOBEX_TEST_PASSWORD"),
]


def form(page: str, form_id: str | None = None) -> tuple[str, dict]:
    """The first (or the named) form on an HTML page: its action URL and hidden/input values."""
    match = re.search(rf'<form[^>]*{f"id=\"{form_id}\"" if form_id else ""}[^>]*>(.*?)</form>', page, re.S)
    if not match:
        raise RuntimeError(f"no form on page: {page[:300]!r}")
    action = html.unescape(re.search(r'action="([^"]+)"', match.group(0)).group(1))
    fields = {html.unescape(n): html.unescape(v)
              for n, v in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', match.group(1))}
    return action, fields


def login(api: str, kind: str, email: str, password: str) -> dict:
    # Keycloak 26 marks its login cookies Secure even over plain HTTP. Browsers
    # still send them to http://localhost (a "secure context"), so do the same.
    def like_a_browser_on_localhost(_response):
        for cookie in browser.cookies.jar:
            cookie.secure = False

    with httpx.Client(follow_redirects=True, timeout=20,
                      event_hooks={"response": [like_a_browser_on_localhost]}) as browser:
        page = browser.get(f"{api}/auth/{kind}/login")
        page.raise_for_status()
        action, fields = form(page.text, "kc-form-login")
        result = browser.post(action, data={**fields, "username": email, "password": password})
        if 'id="kc-form-login"' in result.text:
            raise RuntimeError("Keycloak did not accept the sign-in (wrong password, or user disabled/locked)")
        if kind == "saml":
            # Keycloak answers with an auto-submitting form carrying the signed SAMLResponse.
            acs, saml_fields = form(result.text)
            if "SAMLResponse" not in saml_fields:
                raise RuntimeError(f"Keycloak returned no SAMLResponse: {result.text[:300]!r}")
            result = browser.post(acs, data=saml_fields)
        if result.status_code != 200 or "access_token" not in result.text:
            raise RuntimeError(f"{result.status_code} {result.text[:300]}")
        return result.json()


def claims(access_token: str) -> dict:
    payload = access_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default=os.environ.get("GATEKEEPER_API_URL", "http://localhost:8000"))
    args = parser.parse_args()

    failures = 0
    for kind, email, role, password_var in USERS:
        try:
            token = claims(login(args.api, kind, email, os.environ[password_var])["access_token"])
            ok = token.get("role") == role
            print(f"{'ok  ' if ok else 'FAIL'} {kind:4} {email:28} role={token.get('role')} tenant={token.get('tenant_id', '')[:8]}…")
        except Exception as e:  # report every user, not just the first failure
            ok = False
            print(f"FAIL {kind:4} {email:28} {e}")
        failures += not ok
    print(f"{len(USERS) - failures}/{len(USERS)} logins ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
