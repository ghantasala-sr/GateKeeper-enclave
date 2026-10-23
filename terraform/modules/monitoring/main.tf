# Module C4: watching the Enclave from inside it. Cloud Logging, Cloud Monitoring and
# Managed Service for Prometheus are Google APIs, so the data never leaves Google.
#
# The egress alert turns zero egress from a one-time proof into continuous monitoring:
# if anything inside ever tries to reach a forbidden destination, the firewall's
# DENIED log line raises an incident.

locals {
  prom = "prometheus.googleapis.com"
  ns   = "namespace=\"gatekeeper\""
}

# ---- the firewall: any DENIED egress ----
resource "google_logging_metric" "egress_denied" {
  project     = var.project_id
  name        = "enclave/egress_denied"
  description = "Connections the VPC firewall's deny-all-egress rule dropped."
  filter      = <<-EOT
    logName="projects/${var.project_id}/logs/compute.googleapis.com%2Ffirewall"
    jsonPayload.disposition="DENIED"
    jsonPayload.rule_details.direction="EGRESS"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "src_ip"
      value_type  = "STRING"
      description = "Who tried to get out"
    }
    labels {
      key         = "dest"
      value_type  = "STRING"
      description = "Where to (IP)"
    }
  }
  label_extractors = {
    src_ip = "EXTRACT(jsonPayload.connection.src_ip)"
    dest   = "EXTRACT(jsonPayload.connection.dest_ip)"
  }
}

resource "google_monitoring_alert_policy" "egress_attempt" {
  project      = var.project_id
  display_name = "Enclave: egress attempt (something inside tried to get out)"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "deny-all-egress dropped a connection"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.egress_denied.name}\" AND resource.type=\"gce_subnetwork\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.src_ip"]
      }
      trigger {
        count = 1
      }
    }
  }
  documentation {
    mime_type = "text/markdown"
    content   = "A workload in the Enclave tried to reach a destination the firewall forbids. Find it by `src_ip` (a node IP means a pod on that node). See docs/RUNBOOK.md, \"Egress attempt\"."
  }
  alert_strategy {
    auto_close = "1800s"
  }
  notification_channels = concat([google_monitoring_notification_channel.pubsub.id], var.notification_channels)
}

# ---- the app (Prometheus metrics from the backend, via Managed Service for Prometheus) ----
locals {
  promql_alerts = {
    error_rate = {
      name     = "Gatekeeper: backend error rate over 2%"
      severity = "ERROR"
      query    = "sum(rate(gatekeeper_http_requests_total{${local.ns},status=~\"5..\"}[5m])) / sum(rate(gatekeeper_http_requests_total{${local.ns}}[5m])) > 0.02"
      duration = "300s"
      doc      = "More than 2% of requests failed. A 503 on /chat usually means Vertex AI was unreachable or throttled (see gatekeeper_model_unavailable_total)."
    }
    latency = {
      name     = "Gatekeeper: p95 chat latency over 20s"
      severity = "WARNING"
      query    = "histogram_quantile(0.95, sum by (le) (rate(gatekeeper_http_request_duration_seconds_bucket{${local.ns},route=\"/chat\"}[10m]))) > 20"
      duration = "600s"
      doc      = "Chat answers are slow; usually the model (retries on a throttled shared quota)."
    }
    denied_spike = {
      name     = "Gatekeeper: spike in denied requests (possible attack)"
      severity = "WARNING"
      query    = "sum(rate(gatekeeper_http_requests_total{${local.ns},status=~\"401|403\"}[5m])) * 60 > 30"
      duration = "120s"
      doc      = "Over 30 requests a minute were refused as unauthenticated or unauthorized: forged or stolen tokens, or a misconfigured client."
    }
    forbidden_retrieved = {
      name     = "Gatekeeper: a forbidden chunk was retrieved (must never happen)"
      severity = "CRITICAL"
      query    = "sum(increase(gatekeeper_forbidden_retrieved_total{${local.ns}}[10m])) > 0"
      duration = "0s"
      doc      = "The independent leak check found a retrieved chunk outside the user's tenant or clearance. Treat as a security incident: freeze deploys, read the trace log."
    }
    model_unavailable = {
      name     = "Gatekeeper: the model is unreachable"
      severity = "ERROR"
      query    = "sum(increase(gatekeeper_model_unavailable_total{${local.ns}}[10m])) > 5"
      duration = "0s"
      doc      = "Chats are answering 503 because Vertex AI can't be reached or keeps throttling. Check the restricted-range route, DNS, Workload Identity, and Vertex AI quotas."
    }
  }
}

resource "google_monitoring_alert_policy" "app" {
  for_each     = local.promql_alerts
  project      = var.project_id
  display_name = each.value.name
  combiner     = "OR"
  severity     = each.value.severity
  conditions {
    display_name = each.value.name
    condition_prometheus_query_language {
      query               = each.value.query
      duration            = each.value.duration
      evaluation_interval = "60s"
    }
  }
  documentation {
    mime_type = "text/markdown"
    content   = "${each.value.doc} See docs/RUNBOOK.md."
  }
  notification_channels = concat([google_monitoring_notification_channel.pubsub.id], var.notification_channels)
}

# ---- SLOs: availability and latency, on the backend's own request metrics ----
resource "google_monitoring_custom_service" "gatekeeper" {
  project      = var.project_id
  service_id   = "gatekeeper"
  display_name = "Gatekeeper"
}

resource "google_monitoring_slo" "availability" {
  project             = var.project_id
  service             = google_monitoring_custom_service.gatekeeper.service_id
  slo_id              = "availability"
  display_name        = "99.5% of API requests succeed (not 5xx), 28 days"
  goal                = 0.995
  rolling_period_days = 28
  request_based_sli {
    good_total_ratio {
      total_service_filter = "metric.type=\"${local.prom}/gatekeeper_http_requests_total/counter\" resource.type=\"prometheus_target\" resource.label.\"namespace\"=\"gatekeeper\""
      bad_service_filter   = "metric.type=\"${local.prom}/gatekeeper_http_requests_total/counter\" resource.type=\"prometheus_target\" resource.label.\"namespace\"=\"gatekeeper\" metric.label.\"status\"=monitoring.regex.full_match(\"5..\")"
    }
  }
}

resource "google_monitoring_slo" "chat_latency" {
  project             = var.project_id
  service             = google_monitoring_custom_service.gatekeeper.service_id
  slo_id              = "chat-latency"
  display_name        = "95% of chats answer within 20s, 28 days"
  goal                = 0.95
  rolling_period_days = 28
  request_based_sli {
    distribution_cut {
      distribution_filter = "metric.type=\"${local.prom}/gatekeeper_http_request_duration_seconds/histogram\" resource.type=\"prometheus_target\" resource.label.\"namespace\"=\"gatekeeper\" metric.label.\"route\"=\"/chat\""
      range {
        max = 20
      }
    }
  }
}

# ---- one dashboard ----
resource "google_monitoring_dashboard" "enclave" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "Gatekeeper Enclave"
    mosaicLayout = {
      columns = 12
      tiles = [
        for i, t in [
          { title = "Requests by status (per second)", q = "sum by (status) (rate(gatekeeper_http_requests_total{${local.ns}}[5m]))" },
          { title = "p95 latency by route (s)", q = "histogram_quantile(0.95, sum by (le, route) (rate(gatekeeper_http_request_duration_seconds_bucket{${local.ns}}[10m])))" },
          { title = "Denied requests (401/403 per minute)", q = "sum(rate(gatekeeper_http_requests_total{${local.ns},status=~\"401|403\"}[5m])) * 60" },
          { title = "Chunks retrieved vs FORBIDDEN retrieved (must stay 0)", q = "sum(rate(gatekeeper_chat_chunks_retrieved_total{${local.ns}}[10m])) or sum(rate(gatekeeper_forbidden_retrieved_total{${local.ns}}[10m]))" },
          { title = "Rejected tool calls / model unavailable", q = "sum(rate(gatekeeper_tool_calls_rejected_total{${local.ns}}[10m])) or sum(rate(gatekeeper_model_unavailable_total{${local.ns}}[10m]))" },
          ] : {
          xPos = (i % 2) * 6, yPos = floor(i / 2) * 4, width = 6, height = 4
          widget = {
            title   = t.title
            xyChart = { dataSets = [{ timeSeriesQuery = { prometheusQuery = t.q }, plotType = "LINE" }] }
          }
        }
      ]
    }
  })
}

# ---- where alerts go: a Pub/Sub topic inside the project ----
# Nothing leaves Google: a pager, chat bridge or ticketing system subscribes to it.
# (Email or other channels can be added with var.notification_channels.)
resource "google_pubsub_topic" "alerts" {
  project = var.project_id
  name    = "enclave-alerts"
}

resource "google_pubsub_subscription" "alerts" {
  project                    = var.project_id
  name                       = "enclave-alerts-oncall"
  topic                      = google_pubsub_topic.alerts.id
  message_retention_duration = "604800s" # 7 days
}

# Cloud Monitoring publishes as its notification service agent. Google creates that
# account lazily, so ask for it explicitly before granting it anything.
resource "google_project_service_identity" "monitoring" {
  provider = google-beta
  project  = var.project_id
  service  = "monitoring.googleapis.com"
}

resource "google_pubsub_topic_iam_member" "monitoring_publishes" {
  project = var.project_id
  topic   = google_pubsub_topic.alerts.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_project_service_identity.monitoring.email}"
}

resource "google_monitoring_notification_channel" "pubsub" {
  project      = var.project_id
  display_name = "Enclave alerts (Pub/Sub)"
  type         = "pubsub"
  labels = {
    topic = google_pubsub_topic.alerts.id
  }
}
