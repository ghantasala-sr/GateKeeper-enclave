# Module C6: only images built and signed for this release can run.
#
# The build machine signs each image DIGEST with the attestor's private key (kept in
# Secret Manager; no workload or the bastion can read it). The bastion uploads the
# signature as a Binary Authorization attestation, and GKE refuses to start any image
# without one. Google's own system images are allowed by the global policy.

resource "tls_private_key" "attestor" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

# The signing key, for the build machine (admins only: no IAM grants below).
resource "google_secret_manager_secret" "attestor_key" {
  project   = var.project_id
  secret_id = "gk-attestor-signing-key"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "attestor_key" {
  secret      = google_secret_manager_secret.attestor_key.id
  secret_data = tls_private_key.attestor.private_key_pem
}

resource "google_container_analysis_note" "release" {
  project = var.project_id
  name    = "gatekeeper-release"
  attestation_authority {
    hint {
      human_readable_name = "Gatekeeper release: built and signed by make-bundle.sh"
    }
  }
}

resource "google_binary_authorization_attestor" "release" {
  project = var.project_id
  name    = "gatekeeper-release"
  attestation_authority_note {
    note_reference = google_container_analysis_note.release.name
    public_keys {
      id = "gatekeeper-release-ecdsa"
      pkix_public_key {
        public_key_pem      = tls_private_key.attestor.public_key_pem
        signature_algorithm = "ECDSA_P256_SHA256"
      }
    }
  }
}

resource "google_binary_authorization_policy" "enclave" {
  project                       = var.project_id
  global_policy_evaluation_mode = "ENABLE" # GKE's own system images
  default_admission_rule {
    evaluation_mode         = "REQUIRE_ATTESTATION"
    enforcement_mode        = "ENFORCED_BLOCK_AND_AUDIT_LOG"
    require_attestations_by = [google_binary_authorization_attestor.release.name]
  }
}

# The bastion uploads the build machine's signatures (it can't make them).
resource "google_container_analysis_note_iam_member" "bastion_attaches" {
  project = var.project_id
  note    = google_container_analysis_note.release.name
  role    = "roles/containeranalysis.notes.attacher"
  member  = "serviceAccount:${var.bastion_sa_email}"
}

resource "google_project_iam_member" "bastion_occurrences" {
  project = var.project_id
  role    = "roles/containeranalysis.occurrences.editor"
  member  = "serviceAccount:${var.bastion_sa_email}"
}

resource "google_binary_authorization_attestor_iam_member" "bastion_views" {
  project  = var.project_id
  attestor = google_binary_authorization_attestor.release.name
  role     = "roles/binaryauthorization.attestorsViewer"
  member   = "serviceAccount:${var.bastion_sa_email}"
}

# ...and checks which digests are already attested (so a re-run doesn't conflict).
resource "google_container_analysis_note_iam_member" "bastion_lists" {
  project = var.project_id
  note    = google_container_analysis_note.release.name
  role    = "roles/containeranalysis.notes.occurrences.viewer"
  member  = "serviceAccount:${var.bastion_sa_email}"
}
