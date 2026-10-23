"""Step B16: fail if a Terraform plan opens any path out of the Enclave.

    terraform plan -out plan.out && terraform show -json plan.out > plan.json
    python proof/check_terraform_plan.py plan.json

Reads only the planned values, so CI can check a saved plan without credentials.
Each rule names what it blocks; a violation prints the resource address and why.
"""

import json
import sys

ANYWHERE = {"0.0.0.0/0", "::/0"}


def first(block):
    """Terraform renders nested blocks as lists; most have at most one item."""
    return block[0] if isinstance(block, list) and block else (block if isinstance(block, dict) else {})


def violations(resource_type: str, v: dict) -> list[str]:
    out = []
    if resource_type == "google_compute_router_nat":
        out.append("Cloud NAT is a door to the internet")
    if resource_type == "google_compute_network" and v.get("delete_default_routes_on_create") is not True:
        out.append("the VPC keeps GCP's default 0.0.0.0/0 route (delete_default_routes_on_create must be true)")
    if resource_type == "google_compute_route" and v.get("dest_range") in ANYWHERE:
        out.append(f"route to {v['dest_range']}")
    if resource_type == "google_compute_instance":
        for nic in v.get("network_interface") or []:
            if nic.get("access_config") or nic.get("ipv6_access_config"):
                out.append("VM has an external IP (access_config)")
    if resource_type == "google_compute_address" and v.get("address_type", "EXTERNAL") == "EXTERNAL":
        out.append("reserves an external IP address")
    if resource_type == "google_compute_firewall" and v.get("direction") == "EGRESS" and v.get("allow"):
        if ANYWHERE & set(v.get("destination_ranges") or []):
            out.append("firewall ALLOWS egress to anywhere")
    if resource_type == "google_container_cluster":
        pcc = first(v.get("private_cluster_config"))
        if pcc.get("enable_private_nodes") is not True:
            out.append("GKE nodes would get external IPs (enable_private_nodes must be true)")
        if pcc.get("enable_private_endpoint") is not True:
            out.append("GKE control plane would have a public endpoint (enable_private_endpoint must be true)")
    if resource_type == "google_container_node_pool":
        nc = first(v.get("network_config"))
        if nc.get("enable_private_nodes") is False:
            out.append("node pool overrides the cluster and gives nodes external IPs")
    if resource_type == "google_sql_database_instance":
        ip = first(first(v.get("settings")).get("ip_configuration"))
        if ip.get("ipv4_enabled") is not False:
            out.append("Cloud SQL would get a public IP (ipv4_enabled must be false)")
    return out


def check(plan: dict) -> list[str]:
    problems = []
    for rc in plan.get("resource_changes", []):
        if rc.get("mode") == "data" or rc.get("change", {}).get("actions") == ["delete"]:
            continue  # data sources create nothing; deleting a bad resource is fine
        after = rc.get("change", {}).get("after") or {}
        for why in violations(rc["type"], after):
            problems.append(f"{rc['address']}: {why}")
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    plan = json.load(open(sys.argv[1]))
    problems = check(plan)
    checked = len(plan.get("resource_changes", []))
    if problems:
        print(f"BLOCKED: {len(problems)} way(s) out of the Enclave in this plan:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ok: {checked} resources checked, no path to the internet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
