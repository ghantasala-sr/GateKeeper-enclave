"""Configure Keycloak as Globex's SAML identity provider (stands in for Microsoft Entra ID).

Creates the `globex` realm, a SAML client for Gatekeeper, the 3 roles, and the
3 Globex test users (password: GLOBEX_TEST_PASSWORD in .env). Safe to re-run.
Usage: python -m scripts.setup_keycloak
"""

import os

import httpx
from dotenv import dotenv_values

from app.config import ENV_FILE

env = {**dotenv_values(ENV_FILE), **os.environ}
KEYCLOAK = f"http://localhost:{env.get('KEYCLOAK_PORT', '8080')}"
REALM = "globex"
SP_ENTITY_ID = "urn:gatekeeper:globex"
ACS_URL = "http://localhost:8000/auth/saml/acs"
ROLES = ("employee", "hr_manager", "admin")  # same values as the Entra app roles
USERS = [
    ("employee@globex.example", "Globex", "Employee", "employee"),
    ("hr.manager@globex.example", "Globex", "HR", "hr_manager"),
    ("admin@globex.example", "Globex", "Admin", "admin"),
]


def main() -> None:
    token = httpx.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli",
              "username": "admin", "password": env["KEYCLOAK_ADMIN_PASSWORD"]},
    ).raise_for_status().json()["access_token"]
    kc = httpx.Client(base_url=f"{KEYCLOAK}/admin/realms", headers={"Authorization": f"Bearer {token}"})

    if kc.get(f"/{REALM}").status_code == 404:
        kc.post("", json={"realm": REALM, "enabled": True, "displayName": "Globex"}).raise_for_status()
        print(f"created realm {REALM}")

    for role in ROLES:
        if kc.get(f"/{REALM}/roles/{role}").status_code == 404:
            kc.post(f"/{REALM}/roles", json={"name": role}).raise_for_status()

    client = {
        "clientId": SP_ENTITY_ID,
        "name": "Gatekeeper",
        "protocol": "saml",
        "enabled": True,
        "redirectUris": [ACS_URL],
        "frontchannelLogout": False,
        "attributes": {
            "saml_assertion_consumer_url_post": ACS_URL,
            "saml.server.signature": "true",       # sign the whole Response
            "saml.assertion.signature": "true",    # and the Assertion inside it
            "saml.signature.algorithm": "RSA_SHA256",
            "saml.client.signature": "false",      # Gatekeeper doesn't sign its AuthnRequests
            "saml.force.post.binding": "true",
            "saml_force_name_id_format": "true",
            "saml_name_id_format": "persistent",   # stable opaque ID -> users.external_id
            "saml.encrypt": "false",
        },
        "protocolMappers": [
            {"name": "email", "protocol": "saml", "protocolMapper": "saml-user-property-mapper",
             "config": {"user.attribute": "email", "attribute.name": "email", "attribute.nameformat": "Basic"}},
            {"name": "role", "protocol": "saml", "protocolMapper": "saml-role-list-mapper",
             # single=true: one <Attribute Name="role"> with many values, like Entra sends.
             "config": {"single": "true", "attribute.name": "role", "attribute.nameformat": "Basic"}},
        ],
    }
    existing = kc.get(f"/{REALM}/clients", params={"clientId": SP_ENTITY_ID}).json()
    if existing:
        client_uuid = existing[0]["id"]
        kc.put(f"/{REALM}/clients/{client_uuid}", json={k: v for k, v in client.items() if k != "protocolMappers"}).raise_for_status()
    else:
        kc.post(f"/{REALM}/clients", json=client).raise_for_status()
        client_uuid = kc.get(f"/{REALM}/clients", params={"clientId": SP_ENTITY_ID}).json()[0]["id"]
        print(f"created SAML client {SP_ENTITY_ID}")
    # Create or update our attribute mappers (the client PUT above doesn't touch them).
    current_mappers = {m["name"]: m for m in kc.get(f"/{REALM}/clients/{client_uuid}/protocol-mappers/models").json()}
    for mapper in client["protocolMappers"]:
        if mapper["name"] in current_mappers:
            m = current_mappers[mapper["name"]]
            kc.put(f"/{REALM}/clients/{client_uuid}/protocol-mappers/models/{m['id']}",
                   json={**mapper, "id": m["id"]}).raise_for_status()
        else:
            kc.post(f"/{REALM}/clients/{client_uuid}/protocol-mappers/models", json=mapper).raise_for_status()
    # Drop Keycloak's default "role_list" scope so roles arrive only in our `role` attribute.
    for scope in kc.get(f"/{REALM}/clients/{client_uuid}/default-client-scopes").json():
        if scope["name"] == "role_list":
            kc.delete(f"/{REALM}/clients/{client_uuid}/default-client-scopes/{scope['id']}").raise_for_status()

    for email, first, last, role in USERS:
        found = kc.get(f"/{REALM}/users", params={"username": email, "exact": "true"}).json()
        if found:
            user_id = found[0]["id"]
        else:
            kc.post(f"/{REALM}/users", json={
                "username": email, "email": email, "firstName": first, "lastName": last,
                "enabled": True, "emailVerified": True,
            }).raise_for_status()
            user_id = kc.get(f"/{REALM}/users", params={"username": email, "exact": "true"}).json()[0]["id"]
            print(f"created user {email}")
        kc.put(f"/{REALM}/users/{user_id}/reset-password",
               json={"type": "password", "value": env["GLOBEX_TEST_PASSWORD"], "temporary": False}).raise_for_status()
        # Exactly one Gatekeeper role per user, like an individual Entra app-role assignment.
        current = [r for r in kc.get(f"/{REALM}/users/{user_id}/role-mappings/realm").json() if r["name"] in ROLES]
        if current:
            kc.request("DELETE", f"/{REALM}/users/{user_id}/role-mappings/realm", json=current).raise_for_status()
        kc.post(f"/{REALM}/users/{user_id}/role-mappings/realm",
                json=[kc.get(f"/{REALM}/roles/{role}").json()]).raise_for_status()

    print(f"IdP metadata: {KEYCLOAK}/realms/{REALM}/protocol/saml/descriptor")
    print(f"IdP entity ID: {KEYCLOAK}/realms/{REALM}")


if __name__ == "__main__":
    main()
