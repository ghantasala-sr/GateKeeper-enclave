#!/usr/bin/env bash
# Module C6 (on the bastion): an image that wasn't signed for this release can't run,
# even under the release's own tag.
#
#   ./proof/binauthz-drill.sh
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
token=$(gcloud auth print-access-token)
kubectl config set-credentials bastion --token="$token" >/dev/null
project=$(gcloud config get-value project 2>/dev/null); region=${REGION:-us-central1}
reg="$region-docker.pkg.dev/$project/gatekeeper"; version=$(sed -n 's/^version: *//p' release.lock.yaml)
good=$(awk '$1 == "gatekeeper-backend" {print $3}' images/DIGESTS)
echo "$token" | crane auth login "$region-docker.pkg.dev" -u oauth2accesstoken --password-stdin >/dev/null 2>&1
ns=binauthz-drill-$$; kubectl create namespace $ns >/dev/null
trap 'kubectl delete namespace $ns --wait=false >/dev/null; crane tag "$reg/gatekeeper-backend@$good" "$version" >/dev/null 2>&1' EXIT
fails=0
try() {  # label, image, expect(admitted|denied)
  out=$(kubectl run "t$RANDOM" -n $ns --image="$2" --restart=Never --command -- sleep 5 2>&1)
  if echo "$out" | grep -q "created"; then got=admitted; else got=denied; fi
  printf '  %-58s %-9s %s\n' "$1" "$got" "$([ $got = $3 ] && echo ok || { fails=$((fails+1)); echo 'FAIL'; })"
  [ $got = denied ] && echo "$out" | grep -o "denied by .*" | head -1 | cut -c1-150 | sed 's/^/      /'
}

echo "== 1. tamper: push a modified image under the release's own tag ($version)"
crane mutate "$reg/gatekeeper-backend@$good" --label tampered=true -t "$reg/gatekeeper-backend:$version" >/dev/null 2>&1
bad=$(crane digest "$reg/gatekeeper-backend:$version")
echo "   the tag now points at $bad (the signed image is $good)"

echo "== 2. try to run it"
try "the tampered image, by digest" "$reg/gatekeeper-backend@$bad" denied
try "the release tag (now the tampered image)" "$reg/gatekeeper-backend:$version" denied
try "the signed image, by digest" "$reg/gatekeeper-backend@$good" admitted
try "an image from the internet (if it could even be pulled)" "docker.io/library/busybox@sha256:$(printf '0%.0s' $(seq 64))" denied

echo "== 3. put the tag back on the signed image"
crane tag "$reg/gatekeeper-backend@$good" "$version" >/dev/null && echo "   $version -> $(crane digest "$reg/gatekeeper-backend:$version")"
echo
[ $fails = 0 ] && echo "PASS: only the signed image was admitted" || echo "FAIL: $fails"
exit $fails
