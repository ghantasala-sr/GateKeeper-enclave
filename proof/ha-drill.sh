#!/usr/bin/env bash
# Module C1 (on the bastion, with the cluster in HA mode: terraform apply -var ha=true).
#
#   ./proof/ha-drill.sh drain            drain a node under load: expect zero failed requests
#   ./proof/ha-drill.sh watch-failover   watch the backend through a Cloud SQL failover,
#                                        started meanwhile from an admin's machine with
#                                        `gcloud sql instances failover enclave-pg`
#   ./proof/ha-drill.sh start-watch      the same plus k6, detached (for an SSH session that
#                                        may drop): results in ~/ha-failover.log, /tmp/k6-failover.*
#
# Keycloak runs one dev-mode replica (its database lives in the pod), so it is still a
# single point of failure: the drain picks a node that doesn't host it. Making Keycloak
# highly available needs its own database and clustering (a known gap, see RUNBOOK).
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
ns=gatekeeper; release=gk
kubectl config set-credentials bastion --token="$(gcloud auth print-access-token)" >/dev/null 2>&1 || true

where() { kubectl get pods -n $ns -o wide --no-headers | awk '{print "   " $1, $2, $7}' | column -t; }

case "${1:-}" in
drain)
  echo "== nodes and pods before"
  kubectl get nodes -L topology.kubernetes.io/zone --no-headers | awk '{print "   " $1, $NF}'
  where
  kc_node=$(kubectl get pods -n $ns -l app.kubernetes.io/component=keycloak -o jsonpath='{.items[0].spec.nodeName}')
  node=$(kubectl get pods -n $ns -l 'app.kubernetes.io/component in (backend,frontend)' -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
         | grep -v "^$kc_node\$" | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
  echo "== draining $node (most backend/frontend pods; Keycloak is on $kc_node) under k6, 20 req/s"
  k6 run -q -e DURATION=240s --summary-export /tmp/k6-drain.json proof/k6/availability.js >/tmp/k6-drain.log 2>&1 &
  k6pid=$!; sleep 20
  started=$(date +%s)
  kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=10m 2>&1 | grep -E "evicting|drained" | sed 's/^/   /'
  echo "   drained in $(( $(date +%s) - started ))s"
  wait $k6pid; rc=$?
  kubectl uncordon "$node" >/dev/null
  echo "== pods after"; where
  python3 - <<'PY'
import json
m = json.load(open("/tmp/k6-drain.json"))["metrics"]
r, f, d = int(m["http_reqs"]["count"]), m["http_req_failed"]["value"], m["http_req_duration"]
print(f"   k6: {r} requests, failed {f:.4%} ({round(f * r)}), p95 {d['p(95)']:.0f}ms, max {d['max']:.0f}ms")
PY
  [ $rc = 0 ] && echo "PASS: drain with zero failed requests" || { echo "FAIL"; exit 1; }
  ;;
watch-failover)
  echo "== watching backend readiness and restarts every second (Ctrl-C or 10 minutes)"
  restarts_before=$(kubectl get pods -n $ns -l app.kubernetes.io/component=backend -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{" "}{end}')
  down_since=""; worst=0
  for _ in $(seq 1 600); do
    ready=$(kubectl get pods -n $ns -l app.kubernetes.io/component=backend -o jsonpath='{range .items[*]}{.status.containerStatuses[0].ready}{" "}{end}')
    n_ready=$(echo "$ready" | tr ' ' '\n' | grep -c true)
    now=$(date +%s)
    if [ "$n_ready" = 0 ]; then
      [ -z "$down_since" ] && { down_since=$now; echo "   $(date -u +%T) no backend ready (database unreachable)"; }
    elif [ -n "$down_since" ]; then
      worst=$(( now - down_since )); echo "   $(date -u +%T) ready again after ${worst}s"; down_since=""
      [ "${STOP_AFTER_RECOVERY:-1}" = 1 ] && break
    fi
    sleep 1
  done
  restarts_after=$(kubectl get pods -n $ns -l app.kubernetes.io/component=backend -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{" "}{end}')
  echo "   not-ready window: ${worst}s; restarts before [$restarts_before] after [$restarts_after]"
  [ "$restarts_before" = "$restarts_after" ] && echo "PASS: recovered without restarting" || { echo "FAIL: pods restarted"; exit 1; }
  ;;
start-watch)
  # Detached, so a dropped IAP/SSH session can't stop the drill. Never kill the watchers
  # by a pattern that also matches this command line.
  setsid -f bash "$0" watch-failover > ~/ha-failover.log 2>&1 < /dev/null
  setsid -f k6 run -q -e DURATION=600s --summary-export /tmp/k6-failover.json proof/k6/availability.js > /tmp/k6-failover.log 2>&1 < /dev/null
  sleep 3; echo "watching (log: ~/ha-failover.log); now run the failover"
  ;;
*) sed -n '2,14p' "$0"; exit 2 ;;
esac
