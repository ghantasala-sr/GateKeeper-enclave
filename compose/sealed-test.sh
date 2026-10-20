#!/usr/bin/env bash
# The sealed test: run Gatekeeper with no route out, and prove it still works.
#
#   compose/sealed-test.sh            build, start, check, tear down
#   KEEP=1 compose/sealed-test.sh     leave the stack running afterwards
#
# Every secret is generated for this run only and deleted with it.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
export SEALED_RUN_DIR="$(mktemp -d)"
compose=(docker compose -f "$here/sealed.yml" --env-file "$SEALED_RUN_DIR/sealed.env")

cleanup() {
  if [[ "${KEEP:-}" != 1 ]]; then "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
  rm -rf "$SEALED_RUN_DIR"
}
trap cleanup EXIT

echo "== generating throwaway secrets"
rand() { openssl rand -hex 24; }
{
  for v in POSTGRES_ADMIN_PASSWORD GATEKEEPER_OWNER_PASSWORD APP_DB_PASSWORD KEYCLOAK_ADMIN_PASSWORD \
           ACME_CLIENT_SECRET ACME_TEST_PASSWORD GLOBEX_TEST_PASSWORD SESSION_SECRET; do
    echo "$v=$(rand)"
  done
  # Same format as scripts/gen_signing_key.py: base64 of a PKCS#8 PEM RSA key.
  echo "TOKEN_SIGNING_KEY_B64=$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null | base64 | tr -d '\n')"
} > "$SEALED_RUN_DIR/sealed.env"

# A service-account key Google has never seen: the Vertex AI client tries to fetch
# a token with it, so the failure we observe is the network, not missing config.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null > "$SEALED_RUN_DIR/fake.pem"
python3 - "$SEALED_RUN_DIR" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / "fake-service-account.json").write_text(json.dumps({
    "type": "service_account", "project_id": "sealed-test", "private_key_id": "sealed",
    "private_key": (d / "fake.pem").read_text(), "client_id": "0",
    "client_email": "sealed-test@sealed-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token"}))
PY
chmod 644 "$SEALED_RUN_DIR/fake-service-account.json"   # readable by the container's non-root user

echo "== starting the sealed stack"
"${compose[@]}" up -d --build --wait

echo "== network: $(docker network inspect gk-sealed_sealed --format 'internal={{.Internal}}')"

echo "== checks inside the sealed network"
status=0
"${compose[@]}" exec -T backend python /checks/sealed_checks.py || status=1

echo "== full test suite inside the sealed network (non-superuser owner)"
"${compose[@]}" exec -T backend python -m pytest -q -p no:cacheprovider || status=1

"${compose[@]}" exec -T postgres psql -U postgres_admin -d gatekeeper -Atc \
  "SELECT 'owner ' || rolname || ': superuser=' || rolsuper || ' bypassrls=' || rolbypassrls FROM pg_roles WHERE rolname = 'gatekeeper_owner'"

echo
[[ $status == 0 ]] && echo "SEALED TEST PASSED" || echo "SEALED TEST FAILED"
exit $status
