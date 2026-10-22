#!/usr/bin/env bash
# A7: install Calico, a CNI that enforces NetworkPolicies. Pinned version.
set -euo pipefail
version=v3.32.2
kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/$version/manifests/calico.yaml"
kubectl rollout status daemonset/calico-node -n kube-system --timeout=300s
kubectl rollout status deployment/calico-kube-controllers -n kube-system --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s
