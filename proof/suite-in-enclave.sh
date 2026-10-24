#!/usr/bin/env bash
# Step B15, layer 1 (on the bastion): Gatekeeper's FULL test suite, inside the Enclave,
# against the real Cloud SQL instance (non-superuser owner, real pgvector), on a
# throwaway database. Never the live one: the suite deletes audit rows it wrote, swaps
# SCIM tokens and creates/deletes users.
#
#   gcloud sql databases create gatekeeper_ci --instance enclave-pg   # an admin, first
#   ./proof/suite-in-enclave.sh
#   gcloud sql databases delete gatekeeper_ci --instance enclave-pg   # afterwards
#
# Runs as a Job labelled like the setup Job, so the NetworkPolicies let it reach the
# database and nothing else. It uses the same gatekeeper_app password as production
# (Postgres roles belong to the instance, and migrations set it).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/tools:$PATH" KUBECONFIG="$PWD/.kubeconfig"
project=$(gcloud config get-value project 2>/dev/null)
reg="${REGION:-us-central1}-docker.pkg.dev/$project/gatekeeper"
version=$(sed -n 's/^version: *//p' release.lock.yaml)
# By digest, not tag: Binary Authorization can only check images referenced by digest (Module C6).
backend_image="$reg/gatekeeper-backend@$(awk '$1 == "gatekeeper-backend" {print $3}' images/DIGESTS)"
host=$(gcloud sql instances describe enclave-pg --format='value(ipAddresses[0].ipAddress)')
ns=gatekeeper

kubectl delete job suite-in-enclave -n $ns --ignore-not-found >/dev/null
kubectl apply -f - <<YAML >/dev/null
apiVersion: batch/v1
kind: Job
metadata: { name: suite-in-enclave, namespace: $ns }
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:   # selected by gk-setup-egress: database only
        app.kubernetes.io/name: gatekeeper
        app.kubernetes.io/instance: gk
        app.kubernetes.io/component: setup
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: suite
          image: $backend_image
          securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
          command: ["sh", "-c", "python -m scripts.migrate && python -m scripts.seed && python -m app.rag.ingest && python -m pytest -q -p no:cacheprovider -p no:warnings"]
          env:
            - name: OWNER_PASSWORD
              valueFrom: { secretKeyRef: { name: gatekeeper-db-owner, key: OWNER_PASSWORD } }
            - name: APP_DB_PASSWORD
              valueFrom: { secretKeyRef: { name: gatekeeper-db-owner, key: APP_DB_PASSWORD } }
            - { name: MIGRATION_DATABASE_URL, value: "postgresql://gatekeeper_owner:\$(OWNER_PASSWORD)@$host:5432/gatekeeper_ci" }
            - { name: APP_DATABASE_URL, value: "postgresql://gatekeeper_app:\$(APP_DB_PASSWORD)@$host:5432/gatekeeper_ci" }
            - name: SESSION_SECRET
              valueFrom: { secretKeyRef: { name: gatekeeper-app, key: SESSION_SECRET } }
            - name: TOKEN_SIGNING_KEY_B64
              valueFrom: { secretKeyRef: { name: gatekeeper-app, key: TOKEN_SIGNING_KEY_B64 } }
            - { name: LLM_PROVIDER, value: vertex-gemini }   # the suite uses FakeChatModel; nothing calls Vertex
          resources:
            requests: { cpu: 500m, memory: 768Mi }
            limits: { memory: 1536Mi }
YAML
echo "== running the suite against Cloud SQL ($host, database gatekeeper_ci)"
# Wait for either outcome (kubectl wait with two --for flags waits for both).
for _ in $(seq 1 240); do
  done=$(kubectl get job suite-in-enclave -n $ns -o jsonpath='{.status.succeeded}{.status.failed}')
  [ -n "$done" ] && break; sleep 5
done
kubectl logs job/suite-in-enclave -n $ns | grep -vE "^(Loading|W1|Batches)" | tail -6
kubectl get job suite-in-enclave -n $ns -o jsonpath='{.status.succeeded}' | grep -q 1 && echo "SUITE PASSED inside the Enclave" || { echo "SUITE FAILED"; exit 1; }
