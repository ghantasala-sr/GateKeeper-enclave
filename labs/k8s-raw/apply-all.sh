#!/usr/bin/env bash
# A4-A6: bring the whole lab up on the kind cluster, in order. Safe to re-run.
# Needs the images loaded into kind and Envoy Gateway installed (see 60-gateway.yaml).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
ns=gatekeeper

kubectl apply -f "$here/00-namespace.yaml"
"$here/make-secrets.sh"
kubectl create configmap keycloak-realms -n $ns --from-file="$repo/keycloak/realms/" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$here/05-backend-config.yaml" -f "$here/10-postgres.yaml" -f "$here/40-keycloak.yaml"
kubectl rollout status deployment/postgres -n $ns --timeout=180s

# Setup is a one-shot Job; re-create it so it runs against the current database.
kubectl delete job setup -n $ns --ignore-not-found
kubectl apply -f "$here/20-setup-job.yaml"
kubectl wait --for=condition=complete job/setup -n $ns --timeout=300s

kubectl apply -f "$here/60-gateway.yaml"
kubectl wait --for=condition=Programmed gateway/web -n $ns --timeout=180s
"$here/coredns-rewrite.sh"

kubectl rollout status deployment/keycloak -n $ns --timeout=300s
kubectl apply -f "$here/30-backend-deployment.yaml" -f "$here/31-backend-service.yaml" -f "$here/50-frontend.yaml"
kubectl rollout restart deployment/backend -n $ns    # pick up config changes in the ConfigMap/Secrets
kubectl rollout status deployment/backend -n $ns --timeout=300s
kubectl rollout status deployment/frontend -n $ns --timeout=180s
echo
echo "Up. Now: kubectl port-forward -n envoy-gateway-system svc/gatekeeper-gateway 8088:8088"
echo "     and open http://gatekeeper.localhost:8088"
