# Runbook: Gatekeeper in the Enclave

For whoever is on call. Every command here has been run against the Enclave; the drills in
[`drills/`](drills/) record when and with what result. Design and the security review live in
[`DEPLOYMENT.md`](DEPLOYMENT.md).

## 0. Getting in

```bash
gcloud compute ssh bastion --zone us-central1-a --tunnel-through-iap   # from an admin's laptop
cd ~/gatekeeper-bundle-<version>                                         # the release that's installed
export PATH=$PWD/tools:$PATH KUBECONFIG=$PWD/.kubeconfig
kubectl config set-credentials bastion --token=$(gcloud auth print-access-token)   # tokens last 1 hour
```

| Thing | Name |
|---|---|
| Namespace / Helm release | `gatekeeper` / `gk` |
| Workloads | `gk-backend` (3), `gk-frontend` (2), `gk-keycloak` (1, dev mode) |
| App / login | `https://gatekeeper.enclave.internal` / `https://auth.enclave.internal` (Gateway VIP `10.10.0.100`) |
| Database | Cloud SQL `enclave-pg` (private IP only), database `gatekeeper` |
| Secrets | Secret Manager `gk-*`; backend and setup mount theirs as files (no Kubernetes Secrets) |
| Release in use | `helm list -n gatekeeper`; `curl --cacert config/internal-ca.pem https://gatekeeper.enclave.internal/api/version` |

**First look, whatever the problem:**

```bash
kubectl get pods,deploy -n gatekeeper -o wide
kubectl get events -n gatekeeper --sort-by=.lastTimestamp | tail -20
curl -s --cacert config/internal-ca.pem https://gatekeeper.enclave.internal/api/ready
helm history gk -n gatekeeper | tail -5
```

---

## 1. Install or upgrade

Releases arrive as bundles. Nothing is pulled from the internet.

```bash
# build machine
bundle/make-bundle.sh && gcloud storage cp dist/gatekeeper-bundle-<v>.tgz gs://enclave-gk-bundles/
shasum -a 256 dist/gatekeeper-bundle-<v>.tgz
# bastion
gcloud storage cp gs://enclave-gk-bundles/gatekeeper-bundle-<v>.tgz . && echo "<sha>  gatekeeper-bundle-<v>.tgz" | sha256sum -c
tar xzf gatekeeper-bundle-<v>.tgz && cd gatekeeper-bundle-<v> && ./install-from-bundle.sh
```

The installer:
1. verifies `SHA256SUMS`;
2. pushes the images and checks each pushed digest equals the signed one;
3. uploads the attestations;
4. creates Keycloak's Secret;
5. runs `helm upgrade` by digest: migrations run before the new pods (`pre-upgrade`);
6. runs `helm test`.

Run it detached (`setsid -f ./install-from-bundle.sh > ~/install.log 2>&1`) if your SSH session might drop.

- **Zero downtime is expected.** Rolling updates surge first, terminating pods drain for 15 s, and PDBs hold.
  Verified with k6 (`proof/upgrade-drill.sh`): 0 failed requests.
- **Migrations must only expand** (add nullable columns, new tables). Rename or drop in a *later* release, once no
  old pod can exist.

## 2. Roll back

```bash
helm history gk -n gatekeeper
helm rollback gk <revision> -n gatekeeper --force-conflicts   # see below; no --wait (Helm 4 waits on GKE custom resources forever)
for d in backend frontend keycloak; do kubectl rollout status deployment/gk-$d -n gatekeeper --timeout=10m; done
helm test gk -n gatekeeper --logs
```

**`--force-conflicts`:** Helm 4 applies server-side. If anyone changed a release object with `kubectl` (`patch`, `set image`, `scale`), kubectl now owns those fields and a plain rollback is refused ("conflict … with kubectl-patch"). `--force-conflicts` takes them back. Found on game day: without it the rollback fixed only Keycloak.

A bad image on one Deployment: `kubectl rollout undo deployment/gk-<component> -n gatekeeper`. The schema stays
expanded after a rollback; old code ignores the new columns.

## 3. Restore data (point-in-time)

Use this after a mistake (deleted or overwritten rows). For a database **failure**, Cloud SQL HA fails over by
itself (section 4).

Rehearsed in `proof/restore-drill.sh`: RTO 24 min, RPO seconds.

1. Note the last good moment, in UTC.
2. Clone to it: `gcloud sql instances clone enclave-pg enclave-pg-restore --point-in-time <UTC> --async`. It takes
   about 20 minutes; poll `gcloud sql instances describe enclave-pg-restore --format='value(state)'` until
   `RUNNABLE`.
3. Inspect it from inside the Enclave: a Job like `restore-drill.sh`'s, labelled `app.kubernetes.io/component: setup`
   so the NetworkPolicies allow the database.
4. Repair: copy the lost rows back. Or, for a large loss, point the release at the clone
   (`helm upgrade --reuse-values --set database.host=<clone IP>`) and bring it into Terraform afterwards.
5. Delete the clone. Right after creation it can answer 409 (its first backup is running); retry.

The owner is not a superuser on Cloud SQL, so row-level security applies to it too. Set the tenant context
(`set_config('app.tenant_id', …)`) before touching rows.

## 4. Scale, pause, resume

| What | How |
|---|---|
| More backends | `kubectl scale deployment gk-backend -n gatekeeper --replicas=N`; durable: `backend.replicas` in `config/values-enclave.yaml` |
| Pause (nodes 0, Cloud SQL stopped, bastion off: ~$0.70/day) | `terraform -chdir=terraform/envs/enclave apply -var node_count=0 -var cloudsql_running=false` and `gcloud compute instances stop bastion` |
| Resume | `terraform … apply` (defaults) and `gcloud compute instances start bastion` |
| HA (3 zones, Cloud SQL standby) | `terraform … apply -var ha=true`. After adding zones, wait until the Gateway reports every backend healthy before disruptive work (see the HA drill). |
| Cloud SQL failover | `gcloud sql instances failover enclave-pg` (HA only). Backends go not-ready until it's done (218 s on this tier), with no restarts. A second failover is refused (409) for a few minutes. |

**If a node-pool change hangs** in Terraform ("Still modifying…", but `gcloud container operations list` shows
nothing running):
1. Interrupt it twice. That releases the lock or leaves it.
2. If the lock is left, `terraform force-unlock <generation of gs://…/default.tflock>`.
3. Make the change with `gcloud container node-pools update/resize`, then re-plan.

If your laptop drops off the network during an apply, Terraform writes `errored.tfstate`:
`terraform state push errored.tfstate` after unlocking.

## 5. Rotate

| Secret | How |
|---|---|
| Token signing key | `proof/rotate-signing-key.sh start`, then **after 15 min+**, `finish` (which refuses earlier). Nobody is logged out. |
| A password (database, Keycloak) | Add a version in Secret Manager (`gcloud secrets versions add gk-… --data-file=-`), change it at the source (`ALTER ROLE` / Keycloak), then roll the pods (`kubectl rollout restart deployment/gk-backend`). |
| Gateway certificate (90 days) | `terraform apply` once it's within its 30-day early-renewal window (create-before-destroy), then re-run the installer so the Gateway gets the new name. |

## 6. Alerts

All alerts go to the Pub/Sub topic `enclave-alerts` (subscription `enclave-alerts-oncall`); wire a pager or chat
bridge to it.

### Enclave: egress attempt (CRITICAL)

Something inside tried to reach a destination the firewall forbids.

```bash
gcloud logging read 'logName:"compute.googleapis.com%2Ffirewall" AND jsonPayload.disposition="DENIED"' --freshness=1h \
  --format='table(timestamp,jsonPayload.connection.src_ip,jsonPayload.connection.dest_ip,jsonPayload.connection.dest_port)'
kubectl get pods -A -o wide | grep <node of src_ip>
```

- `src_ip` is a node IP for pod traffic: find what ran on that node at that time (VPC flow logs have the detail).
- An unexpected workload means an incident: isolate the namespace (default-deny NetworkPolicy) before you look
  further.
- Known gap: the bastion's own attempts weren't seen in the firewall log in testing.

### Gatekeeper: a forbidden chunk was retrieved (CRITICAL)

The independent leak check found a retrieved chunk outside the user's tenant or clearance. **Treat as a security
incident.**
1. Freeze deploys.
2. Read the trace log (`kubectl logs -n gatekeeper deploy/gk-backend | grep forbidden`).
3. Compare the RLS policies against `migrations/`.
4. Roll back the last release.

### Gatekeeper: backend error rate over 2% / the model is unreachable

Mostly 503s on `/chat`, meaning Vertex AI is unreachable or throttled.

```bash
kubectl logs -n gatekeeper -l app.kubernetes.io/component=backend -c backend --since=15m | grep -E "aiplatform|429|504|ModelUnavailable"
```

| Cause | Fix |
|---|---|
| 429 / 504 | The shared Gemini quota. Retries with backoff are built in; persistent: check quotas, or switch `llm.vertex.model` |
| Connection errors | the restricted-range route, DNS (`aiplatform.googleapis.com` → `199.36.153.x`), the backend's NetworkPolicy egress to `199.36.153.4/30:443` |
| Auth errors | Workload Identity (`gk-backend` service account annotation, its IAM binding) |

### Gatekeeper: p95 chat latency over 20 s

Usually the model, retrying on throttling. The same log line tells you which.

### Gatekeeper: spike in denied requests

More than 30 refused requests a minute (401/403).

```bash
kubectl logs -n gatekeeper deploy/gk-backend -c backend --since=15m | grep -E " 401 | 403 " | awk '{print $2}' | sort | uniq -c
```

One source and many failures means a forged or stolen token, or a misconfigured client.

## 7. Troubleshooting

| Symptom | Cause seen here | Fix |
|---|---|---|
| Pods `0/1`, readiness fails, **no restarts** | the database is unreachable (by design, liveness ignores the DB) | Cloud SQL state; backend egress NetworkPolicy to `10.50.0.0/16:5432` |
| Pod can't resolve any name | the DNS NetworkPolicy doesn't allow `node-local-dns` (GKE NodeLocal DNSCache) | the chart allows both `kube-dns` and `node-local-dns` |
| Logins 500, `CERTIFICATE_VERIFY_FAILED` | the backend doesn't trust the private CA (a folded PEM in 0.1.9) | `trustedCA` / `SSL_CERT_FILE`; `openssl x509` on the ConfigMap's `ca.crt` |
| Pods not created: `denied by Binary Authorization` | an image without the release attestation, or referenced by tag | deploy by digest from a signed bundle; never re-tag in the registry |
| Pod `CreateContainerConfigError` / mount failed `secrets` | Workload Identity can't read a secret | `gcloud secrets get-iam-policy gk-…`; the KSA annotation |
| `Login refused: email already belongs to a different IdP user` | users re-imported with new IDs | the realm files pin IDs; don't edit them in Keycloak's console |
| Keycloak cookie errors / "restart login cookie" | wrong hostname or HTTP instead of HTTPS | `publicUrls`, `KC_HOSTNAME`, the Gateway listener |
| Chat 503 `Vertex AI returned an error (504)` | Gemini stalls on a busy shared quota | retries; the model choice |
| Gateway paths 502 right after adding zones | the load balancer's new zonal endpoint groups aren't programmed yet | wait for healthy backends |
| A release object edited by hand (a NetworkPolicy, a Deployment's image, replicas) | someone changed the cluster outside Helm | `helm rollback gk <current revision> -n gatekeeper --force-conflicts` re-applies exactly what the release says (without the flag Helm 4 refuses fields kubectl now owns); then `rollout status` |
| Installer stops at "can't check … attestations" | the bastion can't list attestations | `roles/containeranalysis.notes.occurrences.viewer` on the note |

## 8. Teardown

```bash
terraform -chdir=terraform/envs/enclave destroy     # the Enclave
terraform -chdir=terraform/bootstrap destroy        # the project, budget and state bucket (last)
```

Then check that Billing shows nothing running. Cloud SQL clones, buckets with objects, and attestations go with
the project.
