# Drill: secrets from Secret Manager, and a signing-key rotation (Module C5)

**Date:** 2026-10-23 · **Release:** 0.1.8 · **Script:** [`proof/rotate-signing-key.sh`](../../proof/rotate-signing-key.sh)
**Result:** no Gatekeeper secret is a Kubernetes Secret; rotation kept existing sessions working. The old key was
retired about 5 minutes early by the operator, which the script now refuses to do.

## Secrets no longer copied into Kubernetes

After installing 0.1.8 (`secrets.source: secretManager`):

| Check | Result |
|---|---|
| Kubernetes Secrets in `gatekeeper` | only `gatekeeper-keycloak` (Keycloak reads env, not files) and Helm's release records, which hold no secrets because no values do |
| Backend's secrets | 5 files under `/var/run/secrets/gatekeeper` (`app_db_password`, `okta_client_secret`, `session_secret`, `token_signing_key_b64`, `token_verify_keys_b64`), mounted by GKE's Secret Manager CSI driver as the pod's own Workload Identity |
| Backend's environment | no password, key or connection URL |
| Setup Job | runs as `gk-setup`, the only identity that may read the owner password |
| Audit | Secret Manager DATA_READ/ADMIN_READ audit logs on |

The installer deleted the three old app Secrets once 0.1.8 was serving.

## Signing-key rotation

Access tokens live for 15 minutes. The new key signs from `start` onwards. The old key stays **accepted** (and
published in JWKS) until every token it signed has expired, then `finish` retires it.

| Time (UTC) | Step | Result |
|---|---|---|
| 22:44:28 | Employee logs in | token signed by `UFn1yen7…`; JWKS lists 1 key |
| 22:45 | `rotate-signing-key.sh start`: new signing key, old public key added to `gk-token-verify-keys-b64`, rolling restart | |
| 22:46:17 | Same old token | **200**: nobody was logged out |
| 22:46:17 | New login | token signed by `jg6-5aind…`; JWKS lists both, new first |
| 22:54 | `rotate-signing-key.sh finish`: verify-keys back to `none`, rolling restart, old signing-key version disabled | |
| 22:55:43 | Old token | 401 "not signed by a key this server accepts" |
| 22:55:43 | New token | 200 |

**What went wrong:** the old token was valid until 22:59:28, so `finish` at 22:54 cut that session off about
5 minutes early. The operator (me) timed it from a guess rather than from `start`. A user who signed in just before
the rotation would have been logged out early.

**Fix:** `finish` now refuses to run until a full token lifetime, plus 2 minutes for the restart and the drain,
has passed since the new signing key was created. It reads the creation time from Secret Manager, so it doesn't
depend on anyone's memory. Run right after, it said "too early … run finish after 6 minutes" and changed nothing.

Also seen: a request made the instant `rollout status` returned was still answered by an old pod. Terminating pods
keep serving for `drainSeconds` (15 s) on purpose, so a check after a rollout should wait that long.
