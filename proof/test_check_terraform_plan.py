"""Each blocked pattern is caught, and the Enclave's own shapes pass."""

import pytest

from check_terraform_plan import check


def plan(*resources):
    return {"resource_changes": [
        {"address": f"{t}.x", "type": t, "mode": "managed", "change": {"actions": ["create"], "after": after}}
        for t, after in resources
    ]}


GOOD = [
    ("google_compute_network", {"delete_default_routes_on_create": True}),
    ("google_compute_route", {"dest_range": "199.36.153.4/30"}),
    ("google_compute_instance", {"network_interface": [{"access_config": []}]}),
    ("google_compute_address", {"address_type": "INTERNAL"}),
    ("google_compute_firewall", {"direction": "EGRESS", "deny": [{"protocol": "all"}], "allow": [],
                                 "destination_ranges": ["0.0.0.0/0"]}),
    ("google_container_cluster", {"private_cluster_config": [{"enable_private_nodes": True, "enable_private_endpoint": True}]}),
    ("google_sql_database_instance", {"settings": [{"ip_configuration": [{"ipv4_enabled": False}]}]}),
]


def test_the_enclave_shape_passes():
    assert check(plan(*GOOD)) == []


@pytest.mark.parametrize("bad, why", [
    (("google_compute_router_nat", {}), "Cloud NAT"),
    (("google_compute_network", {"delete_default_routes_on_create": False}), "default 0.0.0.0/0 route"),
    (("google_compute_route", {"dest_range": "0.0.0.0/0"}), "route to 0.0.0.0/0"),
    (("google_compute_instance", {"network_interface": [{"access_config": [{"nat_ip": ""}]}]}), "external IP"),
    (("google_compute_address", {"address_type": "EXTERNAL"}), "external IP"),
    (("google_compute_firewall", {"direction": "EGRESS", "allow": [{"protocol": "tcp"}], "destination_ranges": ["0.0.0.0/0"]}), "egress to anywhere"),
    (("google_container_cluster", {"private_cluster_config": [{"enable_private_nodes": True, "enable_private_endpoint": False}]}), "public endpoint"),
    (("google_container_cluster", {"private_cluster_config": []}), "external IPs"),
    (("google_sql_database_instance", {"settings": [{"ip_configuration": [{"ipv4_enabled": True}]}]}), "public IP"),
])
def test_each_way_out_is_blocked(bad, why):
    problems = check(plan(*GOOD, bad))
    assert len(problems) >= 1 and any(why in p for p in problems), problems


def test_deleting_a_bad_resource_is_fine():
    p = plan(("google_compute_router_nat", {}))
    p["resource_changes"][0]["change"]["actions"] = ["delete"]
    assert check(p) == []
