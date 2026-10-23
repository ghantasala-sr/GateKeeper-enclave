#!/usr/bin/env bash
# The Secrets the chart expects (values.yaml -> secrets), with random values, for kind.
# In the Enclave the same Secrets are created from Secret Manager (bundle/).
#
# Each Secret holds only passwords and keys; the chart builds the URLs. Existing
# Secrets are kept (Postgres and Keycloak were initialised with them); --rotate
# replaces them all.
set -euo pipefail
ns=${NAMESPACE:-gatekeeper}
rotate=${1:-}

rand() { openssl rand -hex 24; }
missing() { [[ "$rotate" == --rotate ]] || ! kubectl get secret "$1" -n "$ns" >/dev/null 2>&1; }
put() { kubectl create secret generic "$@" -n "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null; echo "secret/$1 written"; }

kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if missing gatekeeper-db-owner || missing gatekeeper-db-app; then
  owner_pw=$(rand); app_pw=$(rand)
  # The app role's password is in both: setup sets it, the backend logs in with it.
  put gatekeeper-db-owner --from-literal=OWNER_PASSWORD="$owner_pw" --from-literal=APP_DB_PASSWORD="$app_pw"
  put gatekeeper-db-app --from-literal=APP_DB_PASSWORD="$app_pw"
fi
if missing gatekeeper-app; then
  put gatekeeper-app --from-literal=SESSION_SECRET="$(rand)" \
    --from-literal=TOKEN_SIGNING_KEY_B64="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null | base64 | tr -d '\n')"
fi
if missing gatekeeper-keycloak; then
  put gatekeeper-keycloak --from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD="$(rand)" --from-literal=ACME_CLIENT_SECRET="$(rand)" \
    --from-literal=ACME_TEST_PASSWORD="$(rand)" --from-literal=GLOBEX_TEST_PASSWORD="$(rand)"
fi
