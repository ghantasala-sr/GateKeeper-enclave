#!/usr/bin/env bash
# A6: make the public hostnames resolve to the Gateway inside the cluster too.
#
# The backend fetches Keycloak's discovery document, keys and SAML metadata, and
# checks the token issuer, using the SAME URL the browser uses
# (http://auth.gatekeeper.localhost:8088). Without this, that name means
# "this pod" inside a pod. In the Enclave a private Cloud DNS zone does this job.
set -euo pipefail
gateway=gatekeeper-gateway.envoy-gateway-system.svc.cluster.local
corefile=$(kubectl get configmap coredns -n kube-system -o jsonpath='{.data.Corefile}')

if grep -q "gatekeeper.localhost" <<<"$corefile"; then
  echo "CoreDNS already rewrites the gatekeeper.localhost names"; exit 0
fi

rules="    rewrite name exact auth.gatekeeper.localhost $gateway answer auto\n    rewrite name exact gatekeeper.localhost $gateway answer auto"
new=$(awk -v rules="$rules" '{print} /^\.:53 \{/ && !done {print rules; done=1}' <<<"$corefile")
kubectl create configmap coredns -n kube-system --from-literal=Corefile="$new" --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment coredns -n kube-system
kubectl rollout status deployment coredns -n kube-system --timeout=120s
