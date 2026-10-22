#!/usr/bin/env bash
# A5/A6: create the lab's Secrets with random values. Nothing secret is written to disk or git.
#
#   db-owner          Postgres and the setup Job only: the owner's password and URL
#   backend-secrets   the running API: its own (limited) DB login, session secret, signing key
#   keycloak-secrets  Keycloak's admin password, the acme client secret (also read by the
#                     backend) and the test users' passwords
#
# Safe to re-run: a Secret that already exists is kept, because Postgres and Keycloak
# were initialised with its values. --rotate replaces them all (then recreate Postgres
# and Keycloak and re-run setup).
set -euo pipefail
ns=gatekeeper
rotate=${1:-}

rand() { openssl rand -hex 24; }
exists() { [[ "$rotate" != --rotate ]] && kubectl get secret "$1" -n $ns >/dev/null 2>&1; }
apply() { kubectl create secret generic "$@" -n $ns --dry-run=client -o yaml | kubectl apply -f -; }

if exists db-owner && exists backend-secrets; then
  echo "db-owner and backend-secrets already exist"
else
  owner_pw=$(rand); app_pw=$(rand)
  apply db-owner \
    --from-literal=POSTGRES_PASSWORD="$owner_pw" \
    --from-literal=MIGRATION_DATABASE_URL="postgresql://gatekeeper_owner:$owner_pw@postgres:5432/gatekeeper" \
    --from-literal=APP_DB_PASSWORD="$app_pw"
  apply backend-secrets \
    --from-literal=APP_DATABASE_URL="postgresql://gatekeeper_app:$app_pw@postgres:5432/gatekeeper" \
    --from-literal=SESSION_SECRET="$(rand)" \
    --from-literal=TOKEN_SIGNING_KEY_B64="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null | base64 | tr -d '\n')"
fi

if exists keycloak-secrets; then
  echo "keycloak-secrets already exists"
else
  apply keycloak-secrets \
    --from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD="$(rand)" \
    --from-literal=ACME_CLIENT_SECRET="$(rand)" \
    --from-literal=ACME_TEST_PASSWORD="$(rand)" \
    --from-literal=GLOBEX_TEST_PASSWORD="$(rand)"
fi
