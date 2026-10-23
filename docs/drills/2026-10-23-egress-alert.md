# Drill: the egress alert (Module C4)

**Date:** 2026-10-23 · **Release:** 0.1.4 · **Result: passed:** the alert fired about 3½ minutes after the attempt.

## What's being proven

Zero egress isn't only a one-time proof. If anything inside the Enclave ever tries to reach a forbidden
destination, an incident opens.

The chain is:
1. The VPC firewall's `deny-all-egress` rule logs every connection it drops (DENIED).
2. A log-based metric, `enclave/egress_denied`, counts them by source IP.
3. The alert policy "Enclave: egress attempt" fires on any count above 0.
4. The incident is published to the `enclave-alerts` Pub/Sub topic, inside the project. That is where a pager or
   chat bridge subscribes, and nothing leaves Google.

## Run

A pod in a namespace with **no** NetworkPolicies (`egress-test`) made three requests to a destination that has a
route but isn't allowed: Google's restricted range on port 80. Only 443 is open.

Internet destinations would also be blocked, but they have no route at all, so they're dropped before the
firewall sees them and never reach its log. That's the first wall. The routable probe tests the second wall, the
firewall.

| Time (UTC) | Event |
|---|---|
| 17:20:18 | 3 attempts from the egress-test pod, all blocked |
| 17:23:50 | Pub/Sub message received: **INCIDENT OPEN**, "Enclave: egress attempt (something inside tried to get out)", `src_ip=10.10.0.6` (the node the pod ran on), value 1 |

## Findings

- **Firewall logs name the node, not the pod.** Pod traffic to non-cluster ranges leaves with the node's IP
  (`10.10.0.6`). To find the pod, list what runs on that node (`kubectl get pods -A -o wide`) and correlate with
  VPC flow logs, which are on, for the exact time.
- **Attempts from the bastion VM to the same destination were blocked but never appeared in the firewall log.**
  Only GKE nodes' attempts did. Why isn't settled. Until it is, the egress alert covers the cluster's workloads,
  but not the bastion's.
- **Alerts on Prometheus metrics, and request-based SLOs, can only be created once the metric exists** in Cloud
  Monitoring. On a fresh project, apply `module.monitoring` after the first release has run and been scraped.
- **Cloud Monitoring's notification service account is created on demand.** Terraform now asks for it
  explicitly (`google_project_service_identity`) before granting it publish rights on the topic.
