#!/usr/bin/env bash
# A5: create the lab's Secrets with random values. Nothing secret is written to disk or git.
#
#   db-owner         Postgres and the setup Job only: the owner's password and URL
#   backend-secrets  the running API: its own (limited) DB login, session secret, signing key
#
# Safe to re-run: existing Secrets are kept, because Postgres was initialised with
# their passwords. Pass --rotate to replace them (then recreate Postgres and re-run setup).
set -euo pipefail
ns=gatekeeper

if [[ "${1:-}" != --rotate ]] && kubectl get secret db-owner backend-secrets -n $ns >/dev/null 2>&1; then
  echo "Secrets already exist (use --rotate to replace them)"; exit 0
fi

rand() { openssl rand -hex 24; }
owner_pw=$(rand); app_pw=$(rand)
signing_key=$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null | base64 | tr -d '\n')

apply() { kubectl create secret generic "$@" -n $ns --dry-run=client -o yaml | kubectl apply -f -; }

apply db-owner \
  --from-literal=POSTGRES_PASSWORD="$owner_pw" \
  --from-literal=MIGRATION_DATABASE_URL="postgresql://gatekeeper_owner:$owner_pw@postgres:5432/gatekeeper" \
  --from-literal=APP_DB_PASSWORD="$app_pw"

apply backend-secrets \
  --from-literal=APP_DATABASE_URL="postgresql://gatekeeper_app:$app_pw@postgres:5432/gatekeeper" \
  --from-literal=SESSION_SECRET="$(rand)" \
  --from-literal=TOKEN_SIGNING_KEY_B64="$signing_key"
