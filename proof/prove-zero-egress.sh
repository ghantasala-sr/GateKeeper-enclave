#!/usr/bin/env bash
# Step B14 (on the bastion, from the unpacked bundle): prove nothing gets out.
#
#   ./proof/prove-zero-egress.sh
#
# Tries the internet from three places and prints reached/BLOCKED, then shows the
# VPC firewall's DENIED log lines for those attempts (the evidence):
#   1. the bastion itself;
#   2. a pod in the gatekeeper namespace (NetworkPolicies drop it inside the node);
#   3. a pod in a namespace with NO NetworkPolicies, so its packets reach the VPC
#      firewall, which denies and logs them.
# Google APIs (the restricted range) must still work, or nothing could run.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
project=$(gcloud config get-value project 2>/dev/null)
reg="${REGION:-us-central1}-docker.pkg.dev/$project/gatekeeper"
version=$(sed -n 's/^version: *//p' release.lock.yaml)
outside=(https://www.google.com https://api.anthropic.com https://huggingface.co https://pypi.org)
fails=0
row() { printf '  %-52s %s\n' "$1" "$2"; }

echo "== 1. from the bastion"
for u in "${outside[@]}"; do
  if curl -s -o /dev/null -m 5 "$u"; then row "$u" "REACHED  <-- FAIL"; fails=$((fails+1)); else row "$u" "BLOCKED"; fi
done
row "storage.googleapis.com resolves to" "$(getent hosts storage.googleapis.com | awk '{print $1}' | head -1)  (restricted range: 199.36.153.4-7)"
if gcloud storage ls >/dev/null 2>&1; then row "Google APIs (gcloud storage ls)" "work"; else row "Google APIs (gcloud storage ls)" "FAILED  <-- FAIL"; fails=$((fails+1)); fi

probe='import sys, urllib.request as u
bad = 0
for url in sys.argv[1:]:
    try:
        u.urlopen(url, timeout=5); print(f"  {url:52} REACHED  <-- FAIL"); bad += 1
    except Exception as e:
        print(f"  {url:52} BLOCKED ({type(e).__name__})")
sys.exit(bad)'
for ns in gatekeeper egress-proof; do
  if [ $ns = gatekeeper ]; then echo "== 2. from a pod in gatekeeper (NetworkPolicies)"; else echo "== 3. from a pod with no NetworkPolicies (the VPC firewall)"; fi
  kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl run egress-test -n $ns --image="$reg/gatekeeper-backend:$version" --restart=Never --rm -i --quiet \
    --command -- python -c "$probe" "${outside[@]}" || fails=$((fails+1))
done
kubectl delete namespace egress-proof --wait=false >/dev/null

echo "== 4. the VPC firewall's log of those attempts (deny-all-egress, DENIED)"
for i in $(seq 1 12); do   # firewall logs arrive within a minute or two
  logs=$(gcloud logging read 'logName:"compute.googleapis.com%2Ffirewall" AND jsonPayload.disposition="DENIED"' \
    --freshness=15m --limit 10 \
    --format='table[no-heading](timestamp.date("%H:%M:%S"),jsonPayload.rule_details.reference.basename(),jsonPayload.connection.src_ip,jsonPayload.connection.dest_ip,jsonPayload.connection.dest_port)')
  [ -n "$logs" ] && break; sleep 10
done
if [ -n "$logs" ]; then echo "  time      rule              from          to                port"; echo "$logs" | sed 's/^/  /'
else echo "  no DENIED entries yet (try again in a minute)"; fi

echo
[ $fails = 0 ] && echo "ZERO EGRESS: nothing reached the internet; Google APIs work" || echo "FAILED: $fails check(s) reached the internet or lost Google APIs"
exit $fails
