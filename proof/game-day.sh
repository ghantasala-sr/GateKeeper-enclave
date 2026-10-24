#!/usr/bin/env bash
# Module C8: game day (on the bastion). Break three things at once, then fix them using
# only docs/RUNBOOK.md, and time it.
#
#   ./proof/game-day.sh break     # records the start time
#   ./proof/game-day.sh status    # what an operator would look at first
#   ./proof/game-day.sh verify    # all fixed? prints the time since `break`
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
kubectl config set-credentials bastion --token="$(gcloud auth print-access-token)" >/dev/null
ns=gatekeeper; reg="${REGION:-us-central1}-docker.pkg.dev/$(gcloud config get-value project 2>/dev/null)/gatekeeper"
stamp=~/.game-day-start

case "${1:-}" in
break)
  date +%s > $stamp
  echo "== $(date -u +%T) breaking three things"
  kubectl scale deployment gk-keycloak -n $ns --replicas=0 >/dev/null && echo "   1. Keycloak scaled to 0"
  # The backend's egress policy loses its database rule (the first rule: Cloud SQL's range).
  kubectl patch networkpolicy gk-backend-egress -n $ns --type=json -p '[{"op":"remove","path":"/spec/egress/0"}]' >/dev/null \
    && echo "   2. backend NetworkPolicy no longer allows the database"
  kubectl set image deployment/gk-frontend -n $ns frontend="$reg/gatekeeper-frontend:does-not-exist" >/dev/null \
    && echo "   3. frontend set to an image tag that doesn't exist"
  # Database connections that already exist survive a NetworkPolicy change: restart the
  # backends so the breakage is real, as it would be after any pod restart.
  kubectl rollout restart deployment/gk-backend -n $ns >/dev/null && echo "   (backends restarted, as after a node upgrade)"
  ;;
status)
  kubectl get deploy,pods -n $ns -o wide
  kubectl get events -n $ns --sort-by=.lastTimestamp | tail -8 | cut -c1-200
  curl -s -m 5 --cacert config/internal-ca.pem https://gatekeeper.enclave.internal/api/ready; echo
  ;;
verify)
  ok=0
  r=$(curl -s -m 5 --cacert config/internal-ca.pem https://gatekeeper.enclave.internal/api/ready)
  echo "$r" | grep -q ready && echo "   API ready: yes" || { echo "   API ready: NO ($r)"; ok=1; }
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 5 --cacert config/internal-ca.pem https://auth.enclave.internal/realms/acme/.well-known/openid-configuration)
  [ "$c" = 200 ] && echo "   Keycloak: yes" || { echo "   Keycloak: NO ($c)"; ok=1; }
  img=$(kubectl get deployment gk-frontend -n $ns -o jsonpath='{.spec.template.spec.containers[0].image}')
  case "$img" in *does-not-exist*) echo "   frontend image: STILL BAD"; ok=1 ;; *) echo "   frontend image: ${img##*/}" ;; esac
  for d in backend frontend keycloak; do kubectl rollout status deployment/gk-$d -n $ns --timeout=5s >/dev/null 2>&1 || { echo "   gk-$d rollout: not done"; ok=1; }; done
  [ -f $stamp ] && echo "   time since break: $(( ($(date +%s) - $(cat $stamp)) / 60 )) min $(( ($(date +%s) - $(cat $stamp)) % 60 )) s"
  [ $ok = 0 ] && echo "ALL FIXED" || echo "NOT YET"
  exit $ok
  ;;
*) sed -n '2,9p' "$0"; exit 2 ;;
esac
