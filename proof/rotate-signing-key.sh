#!/usr/bin/env bash
# Module C5: rotate the access-token signing key WITHOUT logging anyone out.
# Run by an admin (adding secret versions needs more than the bastion may do).
#
#   proof/rotate-signing-key.sh start    new key signs; the old key is still accepted
#   proof/rotate-signing-key.sh finish   (after TOKEN_TTL_SECONDS, 15 min) retire the old key
#
# Both steps end with a rolling restart of the backend (zero downtime, see the upgrade
# drill): pods read their keys from files mounted from Secret Manager at start.
set -euo pipefail
cd "$(dirname "$0")/.."
project=${PROJECT:-enclave-gk}; zone=${ZONE:-us-central1-a}
bundle=${BUNDLE:-$(sed -n 's/^version: *//p' release.lock.yaml)}
sm() { gcloud secrets "$@" --project "$project"; }
# Needs a Python with `cryptography` (the backend's venv works: PYTHON=gatekeeper/backend/.venv/bin/python).
py=${PYTHON:-python3}
"$py" -c "import cryptography" 2>/dev/null || { echo "need a Python with the cryptography package: set PYTHON=..." >&2; exit 1; }
kid_of() {  # base64 PEM (private or public) on stdin -> RFC 7638 thumbprint, as the app computes it
  "$py" -c '
import base64, hashlib, json, sys
from cryptography.hazmat.primitives import serialization
pem = base64.b64decode(sys.stdin.read())
key = serialization.load_pem_private_key(pem, None).public_key() if b"PRIVATE" in pem else serialization.load_pem_public_key(pem)
n = key.public_numbers()
b64 = lambda i: base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
t = json.dumps({"e": b64(n.e), "kty": "RSA", "n": b64(n.n)}, separators=(",", ":"), sort_keys=True)
print(base64.urlsafe_b64encode(hashlib.sha256(t.encode()).digest()).rstrip(b"=").decode())'
}
restart() {
  gcloud compute ssh bastion --project "$project" --zone "$zone" --tunnel-through-iap --quiet --command "
    cd ~/gatekeeper-bundle-$bundle && export PATH=\$PWD/tools:\$PATH KUBECONFIG=\$PWD/.kubeconfig
    kubectl config set-credentials bastion --token=\$(gcloud auth print-access-token) >/dev/null
    kubectl rollout restart deployment/gk-backend -n gatekeeper >/dev/null
    kubectl rollout status deployment/gk-backend -n gatekeeper --timeout=10m" 2>/dev/null | tail -1
}

case "${1:-}" in
start)
  old=$(sm versions access latest --secret gk-token-signing-key-b64)
  old_public=$(printf '%s' "$old" | base64 -d | openssl pkey -pubout 2>/dev/null | base64 | tr -d '\n')
  echo "current signing key: kid $(printf '%s' "$old" | kid_of)"
  # 1. Keep accepting the old key (its PUBLIC half only: it will never sign again).
  printf '%s' "$old_public" | sm versions add gk-token-verify-keys-b64 --data-file=- >/dev/null
  # 2. A new signing key.
  new=$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null | base64 | tr -d '\n')
  printf '%s' "$new" | sm versions add gk-token-signing-key-b64 --data-file=- >/dev/null
  echo "new signing key:     kid $(printf '%s' "$new" | kid_of) (old key still accepted)"
  restart
  echo "now: new logins get the new key; existing tokens keep working. After 15 minutes: $0 finish"
  ;;
finish)
  printf 'none' | sm versions add gk-token-verify-keys-b64 --data-file=- >/dev/null
  echo "old key retired (gk-token-verify-keys-b64 = none)"
  restart
  # Old signing-key versions stay in Secret Manager (disabled) as history.
  for v in $(sm versions list gk-token-signing-key-b64 --filter="state=ENABLED" --format="value(name)" | tail -n +2); do
    sm versions disable "$v" --secret gk-token-signing-key-b64 >/dev/null && echo "disabled old signing-key version $v"
  done
  ;;
*) sed -n '2,10p' "$0"; exit 2 ;;
esac
