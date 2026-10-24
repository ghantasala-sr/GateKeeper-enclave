# Drill: only signed images run (Module C6)

**Date:** 2026-10-23/24 · **Releases:** 0.1.11–0.1.13 · **Script:** [`proof/binauthz-drill.sh`](../../proof/binauthz-drill.sh)
**Result: passed:** a tampered image under the release's own tag was refused, and the refusal is in the audit log.

## The chain

1. **Build machine** (`bundle/make-bundle.sh`), with internet:
   - builds each image and computes the digest it will have in the registry, from its tar (`crane digest --tarball`);
   - writes an SBOM per image (syft, SPDX JSON);
   - signs a Binary Authorization payload naming `registry/image@digest`, with the attestor key from Secret
     Manager. No workload, and not the bastion, can read that key.
2. **Bastion** (`install-from-bundle.sh`), without internet:
   - pushes the images and refuses to continue unless each pushed digest equals the signed one;
   - uploads the signatures as attestations;
   - deploys by digest (`image:tag@sha256:…`).
3. **GKE** (Binary Authorization, `PROJECT_SINGLETON_POLICY_ENFORCE`) refuses any image without the release
   attestor's attestation. Only Google's own system images are exempt (global policy).

Enforcement was switched on only after the running release was attested (`binauthz_enforce`). Turning it on first
would have refused every pod that restarted.

## The drill (bastion, enforcement on)

| Attempt | Result |
|---|---|
| A modified backend image (a label added), pushed under the release's own tag `0.1.11`, by its new digest | **denied** |
| The release tag itself (now pointing at the modified image) | **denied** |
| An image from the internet | **denied** |
| The signed image, by digest | admitted |

The tag was put back on the signed digest afterwards. Each refusal is in the cluster's audit log
(`cloudaudit.googleapis.com/activity`, `io.k8s.core.v1.pods.create`): *admission webhook
"imagepolicywebhook.image-policy.k8s.io" denied the request: Image … denied by Binary Authorization default admission
rule*.

## Vulnerability scanning

Artifact Analysis scans every pushed image.

| Image | 0.1.11 | 0.1.13 | Fix |
|---|---|---|---|
| backend | 1 critical, 16 high | **0 critical**, 11 high | PyJWT 2.10.1 → 2.15.1 (CVE-2026-102268; PyJWT verifies every access token) |
| frontend | 2 critical, 18 high | **0 critical**, 17 high | Next.js 16.3.8 → 16.4.1 (GHSA-2464-fhr4-w9xw, GHSA-673q-mjm6-r5xr) |
| keycloak 26.2.5 | no findings reported | — | — |

The suite (269) and the frontend build passed on the upgraded packages. The remaining high findings need triage,
mostly in the OS base layers and transitive dependencies. They're a follow-up, not a release blocker.

The SBOMs (backend 235 packages, frontend 360, Keycloak 506) ship in the bundle's `sbom/`.

## What went wrong on the way

- **Re-running the installer conflicted on an existing attestation.** The "already attested?" check used
  `… | grep -q`, which closes the pipe early; with `pipefail` that read as "not attested". After that was fixed
  (output into a variable first), the check *still* found nothing. The bastion lacked
  `containeranalysis.notes.listOccurrences`, and the fix had added `|| true`, which hid the permission error.
  Fixes: the note's occurrence-viewer role for the bastion, and a failed check now stops the install instead of
  being treated as "not attested".
- **Images by tag can't be checked.** Every drill script that started pods by `:tag` now uses the bundle's
  digests (`images/DIGESTS`).
- **Builds are reproducible:** 0.1.12 and 0.1.13 have identical image digests, because only the installer changed,
  so the attestations carried over.
