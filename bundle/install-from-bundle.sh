#!/usr/bin/env bash
# Step B12 (on the bastion, which has NO internet): verify the bundle, load it into
# Artifact Registry, copy secrets from Secret Manager into Kubernetes, and install.
#
#   tar xzf gatekeeper-bundle-<version>.tgz && cd gatekeeper-bundle-<version>
#   ./install-from-bundle.sh
#
# Talks only to Google APIs (restricted range) and to the private GKE control plane.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/tools:$PATH"

project=$(gcloud config get-value project 2>/dev/null)
region=${REGION:-us-central1}; zone=${ZONE:-us-central1-a}
reg="$region-docker.pkg.dev/$project/gatekeeper"
ns=gatekeeper; release=gk   # Workload Identity is bound to gatekeeper/gk-backend
version=$(sed -n 's/^version: *//p' release.lock.yaml)

echo "== 1. verify the bundle"
sha256sum --quiet -c SHA256SUMS && echo "   all $(wc -l < SHA256SUMS) files match SHA256SUMS"

echo "== 2. load images and chart into Artifact Registry ($reg)"
token=$(gcloud auth print-access-token)
echo "$token" | crane auth login "$region-docker.pkg.dev" -u oauth2accesstoken --password-stdin >/dev/null 2>&1
while read -r name tag want; do
  crane push "images/$name.tar" "$reg/$name:$tag" >/dev/null
  got=$(crane digest "$reg/$name:$tag")
  # The registry must now hold exactly the image that was signed on the build machine.
  [ "$got" = "$want" ] || { echo "   $name: pushed $got but the bundle signed $want" >&2; exit 1; }
  echo "   $name:$tag  $got (matches the bundle)"
done < images/DIGESTS

echo "== 2b. attest the images (signatures made on the build machine; Binary Authorization)"
attestor="projects/$project/attestors/gatekeeper-release"
while read -r name tag digest; do
  url="$reg/$name@$digest"
  # Into a variable first: `... | grep -q` would close the pipe early, and with pipefail
  # that reads as "not attested", so an existing attestation was re-created (a conflict).
  # A failed check must stop the install, not read as "not attested".
  existing=$(gcloud container binauthz attestations list --attestor="$attestor" --artifact-url="$url" --format='value(name)') \
    || { echo "   can't check $name's attestations (permissions?)" >&2; exit 1; }
  if [ -n "$existing" ]; then
    echo "   $name: already attested"
  else
    gcloud container binauthz attestations create --artifact-url="$url" --attestor="$attestor" \
      --signature-file="attestations/$name.sig" --public-key-id=gatekeeper-release-ecdsa >/dev/null
    echo "   $name: attested"
  fi
done < images/DIGESTS
echo "$token" | helm registry login "$region-docker.pkg.dev" -u oauth2accesstoken --password-stdin >/dev/null 2>&1
helm push charts/gatekeeper-*.tgz "oci://$reg" 2>&1 | grep -E "^(Pushed|Digest)" | sed 's/^/   /'
chart_version=$(basename charts/gatekeeper-*.tgz .tgz | sed 's/^gatekeeper-//')

echo "== 3. connect to the private cluster"
# A kubeconfig from gcloud's own token, so the bastion needs no auth plugin.
endpoint=$(gcloud container clusters describe enclave-gke --zone "$zone" --format='value(privateClusterConfig.privateEndpoint)')
ca=$(gcloud container clusters describe enclave-gke --zone "$zone" --format='value(masterAuth.clusterCaCertificate)')
export KUBECONFIG="$PWD/.kubeconfig"
kubectl config set-cluster enclave --server="https://$endpoint" >/dev/null
kubectl config set "clusters.enclave.certificate-authority-data" "$ca" >/dev/null
kubectl config set-credentials bastion --token="$token" >/dev/null
kubectl config set-context enclave --cluster=enclave --user=bastion >/dev/null
kubectl config use-context enclave >/dev/null
kubectl get nodes --no-headers | awk '{print "   " $1, $2}'

echo "== 4. secrets (values never printed)"
kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f - >/dev/null
s() { gcloud secrets versions access latest --secret "gk-$1"; }
put() { kubectl create secret generic "$@" -n $ns --dry-run=client -o yaml | kubectl apply -f - >/dev/null; echo "   secret/$1"; }
app_secrets=(gatekeeper-db-owner gatekeeper-db-app gatekeeper-app)
if grep -q "source: secretManager" config/values-enclave.yaml; then
  # The app's secrets are mounted from Secret Manager by the CSI driver; only Keycloak
  # (which reads env) gets a Kubernetes Secret.
  echo "   backend and setup: mounted from Secret Manager (no Kubernetes Secret)"
else
  put gatekeeper-db-owner --from-literal=OWNER_PASSWORD="$(s owner-password)" --from-literal=APP_DB_PASSWORD="$(s app-db-password)"
  put gatekeeper-db-app --from-literal=APP_DB_PASSWORD="$(s app-db-password)"
  put gatekeeper-app --from-literal=SESSION_SECRET="$(s session-secret)" --from-literal=TOKEN_SIGNING_KEY_B64="$(s token-signing-key-b64)"
fi
put gatekeeper-keycloak --from-literal=KC_BOOTSTRAP_ADMIN_PASSWORD="$(s keycloak-admin-password)" \
  --from-literal=ACME_CLIENT_SECRET="$(s acme-client-secret)" --from-literal=ACME_TEST_PASSWORD="$(s acme-test-password)" \
  --from-literal=GLOBEX_TEST_PASSWORD="$(s globex-test-password)"

echo "== 5. install"
db_host=$(gcloud sql instances describe enclave-pg --format='value(ipAddresses[0].ipAddress)')
tls=()
if [ -f config/internal-ca.pem ]; then
  # The Gateway's certificate (Terraform's modules/tls) and the CA the backend must trust.
  cert=$(gcloud compute ssl-certificates list --regions "$region" --filter="name~^enclave-gateway-" \
         --sort-by=~creationTimestamp --format='value(name)' | head -1)
  [ -n "$cert" ] || { echo "   no enclave-gateway-* SSL certificate found (terraform modules/tls, and the bastion's gatekeeperCertReader role)" >&2; exit 1; }
  tls=(--set gateway.tls.preSharedCert="$cert" --set-file trustedCA=config/internal-ca.pem)
  echo "   https: certificate $cert, private CA config/internal-ca.pem"
fi
# Deploy by digest: a tag can be moved, a digest can't.
d() { awk -v n="$1" '$1 == n {print $3}' images/DIGESTS; }
pins=(--set image.backend.digest="$(d gatekeeper-backend)" --set image.frontend.digest="$(d gatekeeper-frontend)"
      --set keycloak.image="$reg/keycloak:26.2.5@$(d keycloak)")
helm upgrade --install $release "oci://$reg/gatekeeper" --version "$chart_version" -n $ns \
  -f config/values-enclave.yaml --set image.tag="$version" --set database.host="$db_host" "${tls[@]}" "${pins[@]}" --timeout 15m
for d in backend frontend keycloak; do kubectl rollout status deployment/$release-$d -n $ns --timeout=10m; done
helm test $release -n $ns --logs | grep -E "Phase|/health|/ready" | sed 's/^/   /'
if grep -q "source: secretManager" config/values-enclave.yaml; then
  # Serving from mounted files now: the old copies must not linger as Kubernetes Secrets.
  kubectl delete secret "${app_secrets[@]}" -n $ns --ignore-not-found | sed 's/^/   /'
fi
echo "== installed gatekeeper $version"
