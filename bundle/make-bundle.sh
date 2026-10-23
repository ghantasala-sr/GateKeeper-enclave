#!/usr/bin/env bash
# Step B11 (on a build machine WITH internet): everything the Enclave needs, in one
# checksummed directory. Nothing is fetched on the other side.
#
#   bundle/make-bundle.sh                  -> dist/gatekeeper-bundle-<version>.tgz
#
# Reads release.lock.yaml. Images are built (or pulled) for the Enclave's platform,
# tools are downloaded and checked against their publishers' checksums.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
lock="$repo/release.lock.yaml"
field() { sed -n "s/^$1: *\([^ #]*\).*/\1/p" "$lock"; }
tool() { sed -n "s/^  $1: *\([^ #]*\).*/\1/p" "$lock"; }

version=$(field version); platform=$(field platform); arch=${platform#linux/}
kubectl_v=$(tool kubectl); helm_v=$(tool helm); crane_v=$(tool crane); k6_v=$(tool k6)
out="$repo/dist/gatekeeper-bundle-$version"
rm -rf "$out" && mkdir -p "$out"/{images,charts,tools,config}
echo "== gatekeeper-bundle-$version ($platform)"

echo "-- images"
docker build -q --platform "$platform" -f "$repo/docker/backend.Dockerfile" -t "gatekeeper-backend:$version" "$repo/gatekeeper" >/dev/null
docker build -q --platform "$platform" -f "$repo/docker/frontend.Dockerfile" --build-arg NEXT_PUBLIC_API_URL=/api \
  -t "gatekeeper-frontend:$version" "$repo/gatekeeper" >/dev/null
docker pull -q --platform "$platform" quay.io/keycloak/keycloak:26.2.5 >/dev/null
docker tag quay.io/keycloak/keycloak:26.2.5 keycloak:26.2.5
for ref in "gatekeeper-backend:$version" "gatekeeper-frontend:$version" keycloak:26.2.5; do
  docker save --platform "$platform" "$ref" -o "$out/images/${ref%%:*}.tar"
  echo "   ${ref}  $(du -h "$out/images/${ref%%:*}.tar" | cut -f1)"
done
printf '%s\n' "gatekeeper-backend $version" "gatekeeper-frontend $version" "keycloak 26.2.5" > "$out/images/TAGS"

echo "-- chart"
helm package "$repo/helm/gatekeeper" -d "$out/charts" 2>/dev/null | sed 's/^/   /'

echo "-- tools (checked against their publishers' checksums)"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -sSfL -o "$out/tools/kubectl" "https://dl.k8s.io/release/$kubectl_v/bin/linux/$arch/kubectl"
echo "$(curl -sSfL "https://dl.k8s.io/release/$kubectl_v/bin/linux/$arch/kubectl.sha256")  $out/tools/kubectl" | shasum -a 256 -c - >/dev/null
curl -sSfL -o "$tmp/helm.tgz" "https://get.helm.sh/helm-$helm_v-linux-$arch.tar.gz"
echo "$(curl -sSfL "https://get.helm.sh/helm-$helm_v-linux-$arch.tar.gz.sha256sum" | cut -d' ' -f1)  $tmp/helm.tgz" | shasum -a 256 -c - >/dev/null
tar xzf "$tmp/helm.tgz" -C "$tmp" "linux-$arch/helm" && mv "$tmp/linux-$arch/helm" "$out/tools/helm"
crane_tgz="go-containerregistry_Linux_$( [ "$arch" = amd64 ] && echo x86_64 || echo arm64 ).tar.gz"
curl -sSfL -o "$tmp/$crane_tgz" "https://github.com/google/go-containerregistry/releases/download/$crane_v/$crane_tgz"
(cd "$tmp" && curl -sSfL "https://github.com/google/go-containerregistry/releases/download/$crane_v/checksums.txt" \
  | grep " $crane_tgz\$" | shasum -a 256 -c - >/dev/null)
tar xzf "$tmp/$crane_tgz" -C "$out/tools" crane
k6_tgz="k6-$k6_v-linux-$arch.tar.gz"
curl -sSfL -o "$tmp/$k6_tgz" "https://github.com/grafana/k6/releases/download/$k6_v/$k6_tgz"
(cd "$tmp" && curl -sSfL "https://github.com/grafana/k6/releases/download/$k6_v/k6-$k6_v-checksums.txt" \
  | grep " $k6_tgz\$" | shasum -a 256 -c - >/dev/null)
tar xzf "$tmp/$k6_tgz" -C "$tmp" && mv "$tmp/k6-$k6_v-linux-$arch/k6" "$out/tools/k6"
chmod +x "$out/tools/"*
echo "   kubectl $kubectl_v, helm $helm_v, crane $crane_v, k6 $k6_v"

echo "-- config and installer"
cp "$repo/config/values-enclave.yaml" "$out/config/"
# The private CA's certificate (public): terraform output -raw internal_ca_cert_pem > config/internal-ca.pem
[ -f "$repo/config/internal-ca.pem" ] && cp "$repo/config/internal-ca.pem" "$out/config/"
cp "$repo/release.lock.yaml" "$out/"
cp "$repo/bundle/install-from-bundle.sh" "$out/" && chmod +x "$out/install-from-bundle.sh"
mkdir -p "$out/proof/k6" && cp "$repo/proof/"*.sh "$out/proof/" && cp "$repo/proof/k6/"*.js "$out/proof/k6/"
cp "$repo/proof/blackbox_check.py" "$repo/keycloak/check_logins.py" "$out/proof/"   # run in-Enclave by checks-in-enclave.sh

(cd "$out" && find . -type f ! -name SHA256SUMS | sort | xargs shasum -a 256 > SHA256SUMS)
# No macOS extended attributes in the archive (GNU tar on the bastion warns about them).
(cd "$repo/dist" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "gatekeeper-bundle-$version.tgz" "gatekeeper-bundle-$version")
echo "== dist/gatekeeper-bundle-$version.tgz  $(du -h "$repo/dist/gatekeeper-bundle-$version.tgz" | cut -f1)"
