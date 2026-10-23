# Step B6: the operators' way in. No external IP; reached only by SSH over IAP.
# It has no internet either, so its tools (kubectl, helm, crane) come in the bundle.

data "google_compute_image" "debian" {
  project = "debian-cloud"
  family  = "debian-12" # includes the gcloud CLI
}

resource "google_compute_instance" "bastion" {
  project      = var.project_id
  name         = "bastion"
  zone         = var.zone
  machine_type = "e2-small" # room for helm/crane while loading the bundle
  tags         = ["bastion"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.debian.self_link
      size  = 30 # the bundle (images + tools) is unpacked here
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = var.subnet_id
    # no access_config block: no external IP
  }

  service_account {
    email  = var.service_account
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE" # SSH access is granted by IAM, not by keys in metadata
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  allow_stopping_for_update = true

  lifecycle {
    # The debian-12 family moves to a new image every few weeks; that must not
    # recreate the bastion (and wipe the unpacked bundle). It's patched in place.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }
}
