#!/usr/bin/env bash
# A7: who can reach what. Run before and after the NetworkPolicies (70-network-policies.yaml).
#
# From a throwaway curl pod (an attacker who got a shell in the namespace) and from a
# real backend pod, try each connection with a short timeout and print reached/BLOCKED.
set -uo pipefail
ns=gatekeeper
p=${PREFIX:-}   # PREFIX=gk- for the Helm release (gk-backend, gk-postgres, ...)

probe_curl() {  # label, curl args
  if kubectl exec -n $ns netpol-probe -- curl -s -o /dev/null -m 4 "${@:2}" 2>/dev/null; then r=reached; else r=BLOCKED; fi
  printf '  %-58s %s\n' "$1" "$r"
}
probe_tcp() {  # label, host, port: did a TCP connection open, whatever the protocol?
  # Speak HTTP to it: a non-HTTP server hangs up (curl exit 52/56) once connected.
  # Only "couldn't connect" (7) or "timed out" (28) mean the packets never got there.
  kubectl exec -n $ns netpol-probe -- curl -s -o /dev/null --connect-timeout 4 -m 5 "http://$2:$3/" 2>/dev/null
  case $? in 7|28) r=BLOCKED ;; *) r=reached ;; esac
  printf '  %-58s %s\n' "$1" "$r"
}
probe_backend() {  # label, python expression that raises if unreachable
  if kubectl exec -n $ns deploy/${p}backend -- python -c "import socket, urllib.request as u; $2" >/dev/null 2>&1; then r=reached; else r=BLOCKED; fi
  printf '  %-58s %s\n' "$1" "$r"
}

kubectl delete pod netpol-probe -n $ns --ignore-not-found >/dev/null   # left over from an earlier run
kubectl run netpol-probe -n $ns --image=curlimages/curl:8.15.0 --restart=Never --command -- sleep 600 >/dev/null
kubectl wait --for=condition=Ready pod/netpol-probe -n $ns --timeout=120s >/dev/null

echo "from a curl pod in the namespace:"
probe_curl "internet (https://www.google.com)" https://www.google.com
probe_tcp "${p}postgres:5432" ${p}postgres 5432
probe_curl "${p}backend:8000/health" http://${p}backend:8000/health
probe_curl "${p}keycloak:8080" http://${p}keycloak:8080/realms/acme
echo "from a backend pod:"
probe_backend "${p}postgres:5432 (its database)" "socket.create_connection(('${p}postgres', 5432), timeout=4)"
probe_backend "Keycloak via the Gateway (OIDC discovery)" "u.urlopen('http://auth.gatekeeper.localhost:8088/realms/acme/.well-known/openid-configuration', timeout=4)"
probe_backend "internet (https://www.google.com)" "u.urlopen('https://www.google.com', timeout=4)"
probe_backend "${p}frontend:3000 (no reason to call it)" "u.urlopen('http://${p}frontend:3000/', timeout=4)"
probe_backend "${p}keycloak:8080 directly (it goes via the Gateway)" "u.urlopen('http://${p}keycloak:8080/realms/acme', timeout=4)"

kubectl delete pod netpol-probe -n $ns --wait=false >/dev/null
