#!/usr/bin/env bash
# Run the login check (all six users) and the black-box security check (access matrix
# + injections, real model) from INSIDE the Enclave, through the HTTPS Gateway, as a pod
# that trusts only the public roots plus the private CA. On the bastion:
#
#   ./proof/checks-in-enclave.sh logins | blackbox
#
# Uses a throwaway namespace (no NetworkPolicies) and a temporary Secret with the test
# users' passwords, both deleted afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
kubectl config set-credentials bastion --token="$(gcloud auth print-access-token)" >/dev/null
project=$(gcloud config get-value project 2>/dev/null)
version=$(sed -n 's/^version: *//p' release.lock.yaml)
# By digest, not tag: Binary Authorization can only check images referenced by digest (Module C6).
image="${REGION:-us-central1}-docker.pkg.dev/$project/gatekeeper/gatekeeper-backend@$(awk '$1 == "gatekeeper-backend" {print $3}' images/DIGESTS)"
what=${1:-logins}; ns=verify-$$
case $what in logins) script=check_logins.py ;; blackbox) script=blackbox_check.py ;; *) echo "logins|blackbox"; exit 2 ;; esac
trap 'kubectl delete namespace $ns --wait=false >/dev/null 2>&1' EXIT

kubectl create namespace $ns >/dev/null
kubectl create configmap checks -n $ns --from-file=proof/check_logins.py --from-file=proof/blackbox_check.py \
  --from-file=ca.crt=config/internal-ca.pem >/dev/null
kubectl create secret generic test-users -n $ns \
  --from-literal=ACME_TEST_PASSWORD="$(gcloud secrets versions access latest --secret gk-acme-test-password)" \
  --from-literal=GLOBEX_TEST_PASSWORD="$(gcloud secrets versions access latest --secret gk-globex-test-password)" >/dev/null
kubectl run checks -n $ns --restart=Never --rm -i --quiet \
  --image="$image" \
  --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}},
    "containers":[{"name":"checks","image":"'"$image"'",
      "command":["sh","-c","mkdir -p /tmp/k && cp /checks/*.py /tmp/k/ && mkdir -p /tmp/keycloak && cp /checks/check_logins.py /tmp/keycloak/ && cat /etc/ssl/certs/ca-certificates.crt /checks/ca.crt > /tmp/ca.pem && cd /tmp/k && SSL_CERT_FILE=/tmp/ca.pem python '"$script"' --api https://gatekeeper.enclave.internal/api"],
      "envFrom":[{"secretRef":{"name":"test-users"}}],
      "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},
      "volumeMounts":[{"name":"checks","mountPath":"/checks"}]}],
    "volumes":[{"name":"checks","configMap":{"name":"checks"}}]}}'
