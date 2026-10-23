# Deploying Gatekeeper into the Enclave

For the platform team that runs the Enclave. It answers the questions a security review asks first, then
walks through an install from an empty Google Cloud project to a verified, running Gatekeeper.

Everything below was done end to end on project `enclave-gk` (us-central1), release **0.1.3**.

---

## The seven questions

### 1. What leaves the network?

Nothing reaches the internet. Two independent controls, each enough on its own:

- **No route.** The VPC is created without GCP's default `0.0.0.0/0` route. Its only route out is to Google's
  restricted API range, `199.36.153.4/30`. There is no Cloud NAT and no Cloud Router, and no VM or node has an
  external IP.
- **Deny and log.** A `deny-all-egress` firewall rule (priority 65000, logging on) blocks every destination except
  that range on port 443 and the Enclave's own internal ranges.

Inside the cluster, default-deny NetworkPolicies (enforced by Dataplane V2) limit each pod to exactly the
connections Gatekeeper makes. The backend may reach its database, Keycloak (through the Gateway), the GKE metadata
server and the restricted range. Every other pod in the namespace gets DNS only.

**Evidence:** `proof/prove-zero-egress.sh`, run on the bastion, tries `www.google.com`, `api.anthropic.com`,
`huggingface.co` and `pypi.org` from the bastion, from a pod under the NetworkPolicies and from a pod with none.
All are blocked, while Google APIs still work through `199.36.153.x`.

Internet packets have no route, so they're dropped before any firewall rule sees them and leave no firewall log.
To show the firewall on its own, the script also sends a request that **is** routable but not allowed (the
restricted range on port 80). The firewall log then records it:

```
firewall:deny-all-egress  10.10.0.4 -> 199.36.153.4:80  DENIED
```

### 2. How does the AI run?

Gemini on Vertex AI (`gemini-3.5-flash`, location `global`), reached at `aiplatform.googleapis.com` through the
restricted range. A private Cloud DNS zone resolves `*.googleapis.com` there. No public AI API is reachable from
anywhere in the Enclave.

Gemini 3.8 Flash was tried first. Here it is only reachable on `global` (the regional `*.rep.googleapis.com`
endpoints aren't served on the restricted range), and for this project most of its calls stalled until the
server deadline (504) or were throttled (429). The model is one value in `config/values-enclave.yaml`. The
backend retries 429/500/503/504 with exponential backoff, and any failure to reach the model ends as a clean
HTTP 503, never a hang.

### 3. Is data used for training?

No. Vertex AI doesn't use customer prompts or responses to train Google's models. Gatekeeper sends the model only
the user's question and the passages that user is already cleared to read. Postgres row-level security filters
every search inside the database, so other tenants' data and documents above the user's clearance never reach
the model.

**Evidence:** `proof/blackbox_check.py`. Through the public API, all six test users ask one question aimed at each
classification, and the Acme employee tries three prompt injections. All 21 run against the real model. Every
chunk retrieved was in the user's own tenant and clearance (`forbidden_retrieved = 0` throughout).

### 4. Are any credentials stored?

There are no service account keys anywhere.

- **Backend pods:** Workload Identity, as `gk-backend@enclave-gk.iam.gserviceaccount.com`, which holds only
  `roles/aiplatform.user`.
- **Nodes and bastion:** their attached service accounts.
- **Application secrets:** the database passwords, session secret, token-signing key and Keycloak's admin and
  client secrets. Terraform generates them and stores them in Secret Manager, in a single region. The install
  script copies them into Kubernetes Secrets, and nobody types or sees them.

They are also in Terraform's state, kept in a versioned, non-public bucket.

| Service account | Roles |
|---|---|
| `gk-backend` | `aiplatform.user` |
| `gk-nodes` | logging/monitoring writers, `artifactregistry.reader` on the one repository |
| `gk-bastion` | `container.developer`, `logging.viewer`, `cloudsql.client`, `artifactregistry.writer` on the repository, read access to the bundle bucket and to the `gk-*` secrets |

### 5. How is it installed?

From a checksummed offline bundle. The bastion has no internet, so everything it runs arrives in the bundle:

```
gatekeeper-bundle-0.1.3/
├── images/        gatekeeper-backend.tar  gatekeeper-frontend.tar  keycloak.tar   (linux/amd64)
├── charts/        gatekeeper-0.1.3.tgz
├── tools/         kubectl v1.36.5  helm v4.3.1  crane v0.22.1   (checked against publishers' checksums)
├── config/        values-enclave.yaml
├── proof/         prove-zero-egress.sh  suite-in-enclave.sh
├── release.lock.yaml
├── SHA256SUMS
└── install-from-bundle.sh
```

The installer verifies `SHA256SUMS` and pushes the images and chart into Artifact Registry with `crane` (no
Docker daemon needed). It then copies the secrets from Secret Manager and runs `helm upgrade --install`. Schema,
seed data and embeddings are created by a post-install hook, and `helm test` checks the backend is live and can
reach its database.

The bundle is dropped in a private bucket in the project and fetched by the bastion over Private Google Access.
That is faster than copying a gigabyte over an IAP SSH tunnel.

### 6. How do operators get in?

Through Identity-Aware Proxy, to a bastion with no external IP. SSH access is granted by IAM (OS Login), not by
keys in metadata.

The GKE control plane has a private endpoint only, open to the main subnet. Gatekeeper itself is served by an
internal Application Load Balancer (a GKE Gateway) on `10.10.0.100`. Nothing has a public endpoint.

### 7. How is it proven?

| Check | Result |
|---|---|
| Zero egress (`prove-zero-egress.sh`) | Internet blocked from the bastion and from pods under, and without, NetworkPolicies; firewall DENIED log; Google APIs work |
| Gatekeeper's full test suite inside the Enclave (`suite-in-enclave.sh`) | **258 passed, 5 skipped**, against the real Cloud SQL instance (non-superuser owner, real pgvector), on a throwaway database |
| Black-box security checks (`blackbox_check.py`) | 21/21 with Gemini; nothing outside a user's tenant or clearance retrieved |
| Blueprint check (`check_terraform_plan.py`) | The plan for all 91 resources has no NAT, no `0.0.0.0/0` route, no external IP, no public GKE endpoint and no public Cloud SQL IP. CI runs its 11 tests on every push. |
| Logins | All six test users, through Keycloak (OIDC for Acme, SAML for Globex) |

---

## What gets built

| Piece | Details |
|---|---|
| Network | `enclave-vpc`: main subnet `10.10.0.0/20` (flow logs, Private Google Access), pods `10.20.0.0/16`, services `10.30.0.0/20`, proxy-only `10.40.0.0/23`, Cloud SQL via Private Services Access `10.50.0.0/16` |
| DNS | Private zones: `googleapis.com`, `pkg.dev` and `gcr.io` point to the restricted range; `gatekeeper.localhost` points to the Gateway |
| GKE | `enclave-gke`, zonal: private nodes and endpoint, Dataplane V2, Workload Identity, Gateway API; 2 × e2-standard-2 |
| Cloud SQL | `enclave-pg`: PostgreSQL 17, db-f1-micro, private IP only, TLS required, backups with point-in-time recovery |
| Registry | Artifact Registry `gatekeeper` (images and the chart as an OCI artifact) |
| Bastion | e2-small, Debian 12, no external IP, OS Login, shielded VM |

---

## Install, from an empty project

Prerequisites: Terraform ≥ 1.9, Docker, Helm and the gcloud CLI on a build machine with internet access, and an
account that can create projects and link a billing account.

**1. Project, budget and state bucket** (once):

```bash
cd terraform/bootstrap
terraform init
terraform apply -var billing_account=XXXXXX-XXXXXX-XXXXXX
```

This creates the project with no default network, budget alerts at $10/$25/$50/$100, and the state bucket.

**2. The Enclave** (about 20 minutes, mostly GKE and Cloud SQL):

```bash
cd terraform/envs/enclave
terraform init -backend-config="bucket=enclave-gk-tfstate"
terraform plan -out plan.out
terraform show -json plan.out > plan.json
python ../../../proof/check_terraform_plan.py plan.json   # must print "no path to the internet"
terraform apply plan.out
```

**3. Build the bundle and drop it in** (build machine):

```bash
bundle/make-bundle.sh
gcloud storage cp dist/gatekeeper-bundle-0.1.3.tgz gs://enclave-gk-bundles/
shasum -a 256 dist/gatekeeper-bundle-0.1.3.tgz       # note it for step 4
```

**4. Install** (on the bastion):

```bash
gcloud compute ssh bastion --zone us-central1-a --tunnel-through-iap
```

Then, on the bastion:

```bash
gcloud storage cp gs://enclave-gk-bundles/gatekeeper-bundle-0.1.3.tgz .
echo "<sha256>  gatekeeper-bundle-0.1.3.tgz" | sha256sum -c
tar xzf gatekeeper-bundle-0.1.3.tgz && cd gatekeeper-bundle-0.1.3
./install-from-bundle.sh
./proof/prove-zero-egress.sh
```

**5. Reach it** (operator laptop):

```bash
gcloud compute ssh bastion --zone us-central1-a --tunnel-through-iap -- -N -L 8088:10.10.0.100:8088
```

Then open `http://gatekeeper.localhost:8088`. Browsers resolve `*.localhost` to the laptop and treat it as a
secure context, so Keycloak's `Secure` cookies work over plain HTTP. Inside the VPC, the private DNS zone sends
the same names to the Gateway, so the browser and the backend agree on one issuer URL. The test users'
passwords are in Secret Manager (`gk-acme-test-password`, `gk-globex-test-password`).

**Upgrades** follow the same path: change `release.lock.yaml`, build the new bundle, and run the installer
again. `helm history gk -n gatekeeper` and `helm rollback` work as usual.

---

## Between sessions

Pause the parts that bill by the hour. Everything else is kept, so resuming takes minutes:

```bash
terraform -chdir=terraform/envs/enclave apply -var node_count=0 -var cloudsql_running=false
gcloud compute instances stop bastion --zone us-central1-a
```

To resume, re-apply with the defaults and start the bastion. To remove the whole Enclave:

```bash
terraform -chdir=terraform/envs/enclave destroy
```

Paused, about $0.70/day remains: the internal load balancer's forwarding rule plus storage. Running, about
$0.25/hour.

---

## Known limits (production requirements)

| Gap | Why, and the fix |
|---|---|
| **VPC Service Controls not enforced** | A perimeter needs a Google Cloud Organization, and this project has none (personal account). The network is still zero-egress. Production: put the project in an Organization, then add a perimeter around `aiplatform`, `artifactregistry`, `storage`, `secretmanager`, `logging`, `monitoring`, `container` and `sqladmin`, starting in dry-run mode. |
| **Plain HTTP inside the Enclave** | Operators reach it over the IAP tunnel, which is encrypted, but the hop from the internal load balancer to the pods is HTTP. Production: internal HTTPS on the Gateway, with certificates from a private CA (Part C). |
| **Keycloak runs `start-dev`, 1 replica** | Its database lives in the pod. The realm files pin user IDs, so a restart re-imports the same users. Production: 2+ replicas on their own Cloud SQL database. |
| **Zonal, single-instance** | GKE and Cloud SQL are zonal to stay inside the free tier. Production: a regional cluster and Cloud SQL with high availability. |
| **No SBOM yet** | The bundle is checksummed, but it doesn't yet ship an SBOM or signed images (Part C). |
