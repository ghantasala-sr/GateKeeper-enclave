#!/usr/bin/env bash
# Install (or upgrade) Gatekeeper on the kind cluster with the Helm chart.
#
# Expects the labs' cluster: Calico (labs/k8s-raw/calico.sh), Envoy Gateway installed,
# and the images loaded into kind. Safe to re-run.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
ns=gatekeeper
release=gk   # values-local.yaml's NetworkPolicy peers name this release's Gateway (gk-gateway)

# Cluster-level pieces the chart doesn't own: the GatewayClass, and in-cluster DNS for
# the public names (in the Enclave: a private Cloud DNS zone).
kubectl apply -f - <<'YAML'
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata: { name: envoy }
spec: { controllerName: gateway.envoyproxy.io/gatewayclass-controller }
YAML
"$repo/labs/k8s-raw/coredns-rewrite.sh"

"$here/local-secrets.sh"
helm upgrade --install $release "$here/gatekeeper" -n $ns -f "$here/gatekeeper/values-local.yaml" --timeout 10m
for d in backend frontend keycloak; do kubectl rollout status deployment/$release-$d -n $ns --timeout=5m; done
helm test $release -n $ns --logs
echo
echo "Up. Now: kubectl port-forward -n envoy-gateway-system svc/gatekeeper-gateway 8088:8088"
echo "     and open http://gatekeeper.localhost:8088"
