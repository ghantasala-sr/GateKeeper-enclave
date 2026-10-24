#!/usr/bin/env bash
# Module C3: a timed point-in-time restore drill. Run from an operator laptop (cloning
# needs Cloud SQL admin rights, which the bastion deliberately doesn't have); every
# database step runs INSIDE the Enclave, as a Job, through the bastion.
#
#   proof/restore-drill.sh [bundle-version]
#
# 1. "Accidentally" delete every chunk of one document (ACME-HR-01) in production.
# 2. Clone the instance to the moment just BEFORE the deletion (point-in-time recovery).
# 3. Prove the chunks exist on the clone and are still missing in production.
# 4. Repair production from the clone (copy the lost rows back), and prove it.
# 5. Delete the clone.
# Targets: RPO 15 minutes (PITR loses at most seconds), RTO 1 hour.
set -euo pipefail
cd "$(dirname "$0")/.."
project=${PROJECT:-enclave-gk}; zone=${ZONE:-us-central1-a}
version=${1:-$(sed -n 's/^version: *//p' release.lock.yaml)}
instance=enclave-pg; clone=${RESUME_CLONE:-"enclave-pg-restore-$(date +%H%M)"}
# By digest, not tag: Binary Authorization can only check images referenced by digest (Module C6).
backend_image="us-central1-docker.pkg.dev/$project/gatekeeper/gatekeeper-backend@$(gcloud artifacts docker images describe \
  "us-central1-docker.pkg.dev/$project/gatekeeper/gatekeeper-backend:$version" --project "$project" --format='value(image_summary.digest)')"
t0=${RESUME_T0:-$(date +%s)}; stamp() { printf '[%5ss] %s\n' $(( $(date +%s) - t0 )) "$*"; }
# RESUME_CLONE, RESUME_T0, RESUME_INCIDENT, RESUME_LAST_GOOD: pick up an interrupted drill
# after its incident, keeping the original clock (the clone is already being created).

# Shared by every step: connect as the owner inside Acme's RLS context (the owner is
# not a superuser on Cloud SQL, so row-level security applies to it too).
PRELUDE=$(cat <<'PY'
import asyncio, os
import asyncpg
DOC = "ACME-HR-01"
COUNT = "SELECT count(*) FROM chunks WHERE document_id = (SELECT id FROM documents WHERE reference = $1)"
async def conn(host):
    c = await asyncpg.connect(host=host, user="gatekeeper_owner", password=os.environ["OWNER_PASSWORD"],
                              database="gatekeeper", ssl="require")
    tid = await c.fetchval("SELECT id FROM tenants WHERE name = 'Acme Corp'")
    await c.execute("SELECT set_config('app.tenant_id', $1, false), set_config('app.role', 'admin', false)", str(tid))
    return c
PY
)

# Run Python (from stdin) in a short-lived Job in the gatekeeper namespace. It is
# labelled like the setup Job, so the NetworkPolicies allow the database and nothing
# else. The code travels base64-encoded, so no shell on the way expands anything.
in_enclave() {  # job-name, DB_HOST, [OTHER_HOST]  < python
  local name=$1 host=$2 other=${3:-} code
  code=$(printf '%s\n%s' "$PRELUDE" "$(cat)" | base64 | tr -d '\n')
  gcloud compute ssh bastion --project "$project" --zone "$zone" --tunnel-through-iap --quiet --command "
    set -e; cd ~/gatekeeper-bundle-$version; export PATH=\$PWD/tools:\$PATH KUBECONFIG=\$PWD/.kubeconfig
    kubectl config set-credentials bastion --token=\$(gcloud auth print-access-token) >/dev/null
    kubectl delete job $name -n gatekeeper --ignore-not-found >/dev/null
    kubectl apply -f - >/dev/null <<'YAML'
apiVersion: batch/v1
kind: Job
metadata: { name: $name, namespace: gatekeeper }
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels: { app.kubernetes.io/name: gatekeeper, app.kubernetes.io/instance: gk, app.kubernetes.io/component: setup }
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: drill
          image: $backend_image
          securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
          command: [python, -c, 'import base64, os; exec(base64.b64decode(os.environ[\"CODE\"]))']
          env:
            - name: OWNER_PASSWORD
              valueFrom: { secretKeyRef: { name: gatekeeper-db-owner, key: OWNER_PASSWORD } }
            - { name: DB_HOST, value: '$host' }
            - { name: OTHER_HOST, value: '$other' }
            - { name: CODE, value: '$code' }
YAML
    for _ in \$(seq 1 80); do s=\$(kubectl get job $name -n gatekeeper -o jsonpath='{.status.succeeded}{.status.failed}'); [ -n \"\$s\" ] && break; sleep 3; done
    kubectl logs job/$name -n gatekeeper" 2>/dev/null | grep -v "NumPy\|increasing_the_tcp\|^WARNING" | sed 's/^/          /'
}

prod_ip=$(gcloud sql instances describe $instance --project "$project" --format='value(ipAddresses[0].ipAddress)')
if [ -z "${RESUME_CLONE:-}" ]; then
stamp "production: $instance ($prod_ip)"
in_enclave drill-count "$prod_ip" <<'PY'
async def main():
    c = await conn(os.environ["DB_HOST"])
    print("ACME-HR-01 chunks in production:", await c.fetchval(COUNT, DOC))
asyncio.run(main())
PY

last_good=$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")')
sleep 5
stamp "INCIDENT: every chunk of ACME-HR-01 deleted by mistake (last good moment $last_good)"
incident=$(date +%s)
in_enclave drill-delete "$prod_ip" <<'PY'
async def main():
    c = await conn(os.environ["DB_HOST"])
    await c.execute("DELETE FROM chunks WHERE document_id = (SELECT id FROM documents WHERE reference = $1)", DOC)
    print("ACME-HR-01 chunks in production after the incident:", await c.fetchval(COUNT, DOC))
asyncio.run(main())
PY

stamp "restore: cloning $instance to $last_good as $clone (point-in-time recovery)"
gcloud sql instances clone $instance "$clone" --point-in-time "$last_good" --project "$project" --async --quiet >/dev/null
else
  incident=$RESUME_INCIDENT; last_good=$RESUME_LAST_GOOD
  stamp "resuming the drill: restore $clone (to $last_good) already started"
fi
# A clone can take longer than gcloud's own 10-minute wait: poll until it's serving.
until [ "$(gcloud sql instances describe "$clone" --project "$project" --format='value(state)' 2>/dev/null)" = RUNNABLE ]; do sleep 20; done
clone_ip=$(gcloud sql instances describe "$clone" --project "$project" --format='value(ipAddresses[0].ipAddress)')
stamp "restored instance ready ($clone_ip, private IP only)"

in_enclave drill-verify "$clone_ip" <<'PY'
async def main():
    c = await conn(os.environ["DB_HOST"])
    print("ACME-HR-01 chunks on the restored instance:", await c.fetchval(COUNT, DOC))
asyncio.run(main())
PY

stamp "repair: copying the lost rows from the restored instance back into production"
in_enclave drill-repair "$prod_ip" "$clone_ip" <<'PY'
async def main():
    src, dst = await conn(os.environ["OTHER_HOST"]), await conn(os.environ["DB_HOST"])
    rows = await src.fetch("SELECT id, document_id, tenant_id, classification, content, embedding::text AS e FROM chunks "
                           "WHERE document_id = (SELECT id FROM documents WHERE reference = $1)", DOC)
    await dst.executemany("INSERT INTO chunks (id, document_id, tenant_id, classification, content, embedding) "
                          "VALUES ($1, $2, $3, $4, $5, $6::vector) ON CONFLICT (id) DO NOTHING",
                          [(r["id"], r["document_id"], r["tenant_id"], r["classification"], r["content"], r["e"]) for r in rows])
    print("copied back:", len(rows), "| ACME-HR-01 chunks in production now:", await dst.fetchval(COUNT, DOC))
asyncio.run(main())
PY
repaired=$(date +%s)
stamp "RECOVERED: RTO $(( (repaired - incident) / 60 )) min (target 60); RPO: data restored as of $last_good, seconds before the incident (target 15 min)"

stamp "cleanup: deleting $clone"
# Right after creation the clone may still be busy (its first backup); retry until it isn't.
until gcloud sql instances delete "$clone" --project "$project" --quiet >/dev/null 2>&1; do sleep 30; done
stamp "done"
