#!/usr/bin/env bash
# Module C2 (on the bastion, from the NEW bundle's directory): upgrade, then roll
# back, each under steady load, and require ZERO failed requests both times.
#
#   cd ~/gatekeeper-bundle-<new> && ./proof/upgrade-drill.sh
#
# k6 sends 20 requests a second through the real Gateway (frontend, API, database
# readiness, Keycloak) for the whole of each step.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
ns=gatekeeper; release=gk
kubectl config set-credentials bastion --token="$(gcloud auth print-access-token)" >/dev/null 2>&1 || true
result=0

under_load() {  # label, seconds, command...
  local label=$1 secs=$2; shift 2
  echo "== $label (k6: 20 req/s for ${secs}s)"
  k6 run -q -e DURATION="${secs}s" --summary-export "/tmp/k6-$label.json" proof/k6/availability.js >"/tmp/k6-$label.log" 2>&1 &
  local k6pid=$!
  sleep 15                                   # traffic flowing before the change starts
  local started=$(date +%s)
  "$@" 2>&1 | grep -E "STATUS|REVISION|Rollback|rolled out|installed|Phase" | sed 's/^/   /'
  echo "   change finished in $(( $(date +%s) - started ))s; k6 keeps going until it ends"
  wait $k6pid; local rc=$?
  python3 - "/tmp/k6-$label.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))["metrics"]
reqs = int(m["http_reqs"]["count"]); failed = m["http_req_failed"]["value"]
d = m["http_req_duration"]
print(f"   k6: {reqs} requests, failed {failed:.4%} ({round(failed * reqs)}), "
      f"p95 {d['p(95)']:.0f}ms, max {d['max']:.0f}ms")
PY
  [ $rc = 0 ] && echo "   PASS: zero failed requests" || { echo "   FAIL (k6 thresholds)"; result=1; }
}

from=$(helm history $release -n $ns --max 1 -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])')
echo "release $release is at revision $from ($(helm list -n $ns -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["app_version"])'))"

under_load upgrade 240 ./install-from-bundle.sh
kubectl get pods -n $ns -l app.kubernetes.io/component=backend -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u | sed 's/^/   now running: /'

# Not `helm rollback --wait`: Helm 4 also waits on GKE's custom resources (PodMonitoring,
# load balancer policies), which never report "ready" in its terms. Wait on the rollouts.
under_load rollback 180 sh -c "helm rollback $release $from -n $ns && for d in backend frontend keycloak; do kubectl rollout status deployment/$release-\$d -n $ns --timeout=10m; done"
kubectl get pods -n $ns -l app.kubernetes.io/component=backend -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u | sed 's/^/   now running: /'

exit $result
