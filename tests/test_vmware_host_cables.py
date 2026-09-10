"""
Cables from the CDP/LLDP neighbors an ESXi host reports for its physical interfaces.

The vcsim captures carry no CDP/LLDP data, so the parts which turn a reported neighbor
into a cable are tested against a hand built inventory. What the vcsim run has to prove
is that the feature stays completely out of the way while `sync_host_cables` is disabled.
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBCable, NBDevice, NBInterface, NBTag
from module.sources import instantiate_sources
from module.sources.vmware.connection import VMWareHandler


def cdp_hint(system_name=None, device_id=None, port_id=None):
    """a QueryNetworkHint() result of an interface which sees a CDP neighbor"""

    return SimpleNamespace(
        connectedSwitchPort=SimpleNamespace(systemName=system_name, devId=device_id, portId=port_id),
        lldpInfo=None
    )


def lldp_hint(port_id=None, **parameters):
    """a QueryNetworkHint() result of an interface which sees an LLDP neighbor"""

    return SimpleNamespace(
        connectedSwitchPort=None,
        lldpInfo=SimpleNamespace(
            portId=port_id,
            parameter=[SimpleNamespace(key=key, value=value) for key, value in parameters.items()]
        )
    )


def termination(object_id, object_type="dcim.interface"):
    return {"object_type": object_type, "object_id": object_id}


@pytest.fixture
def cable_source(inventory):
    """
    A VMware source handler with nothing but the state the cable code touches, on the
    fresh in-memory inventory. Building it without __init__ keeps vCenter out of the way.
    """
    source = object.__new__(VMWareHandler)
    source.inventory = inventory
    source.name = "test"
    source.source_tag = "Source: test"
    source.cable_index = None

    inventory.add_update_object(NBTag, data={"name": source.source_tag})

    return source


@pytest.fixture
def netbox_interface(inventory, cable_source):
    """Returns a function adding a device interface which already exists in NetBox."""

    devices = {}

    def _add(device_name, interface_name, nb_id):
        device = devices.get(device_name)
        if device is None:
            device = inventory.add_object(NBDevice, data={"name": device_name}, source=cable_source)
            devices[device_name] = device

        interface = inventory.add_object(NBInterface, data={"name": interface_name, "device": device},
                                         source=cable_source)
        interface.nb_id = nb_id
        interface.is_new = False

        return interface

    return _add


# --- interface name variants -------------------------------------------------------------------

@pytest.mark.parametrize("reported, expected", [
    ("FastEthernet0/16", "Fa0/16"),
    ("Fa0/16", "FastEthernet0/16"),
    ("Te1/0/1", "TenGigabitEthernet1/0/1"),
    ("Te1/0/1", "TenGigE1/0/1"),
    ("TenGigE0/0/1", "Te0/0/1"),
    ("Twe1/0/5", "TwentyFiveGigabitEthernet1/0/5"),
    ("TwentyFiveGigabitEthernet1/0/5", "TF1/0/5"),
    ("XGigabitEthernet0/0/14", "XGE0/0/14"),
    ("GE1/0/1", "GigabitEthernet1/0/1"),
    ("GigabitEthernet1/0/1", "Gi1/0/1"),
    ("Ethernet1/1", "Eth1/1"),
    ("Eth1/1", "Ethernet1/1"),
])
def test_interface_name_variants_contain_the_other_spelling(reported, expected):
    assert expected in VMWareHandler.get_interface_name_variants(reported)


def test_interface_name_variants_start_with_the_reported_name_and_are_unique():
    variants = VMWareHandler.get_interface_name_variants("FastEthernet0/16")

    assert variants[0] == "FastEthernet0/16"
    assert len(variants) == len(set(variants))


@pytest.mark.parametrize("reported", [None, "", "   "])
def test_interface_name_variants_of_an_unreported_port(reported):
    assert VMWareHandler.get_interface_name_variants(reported) == []


def test_interface_name_variants_do_not_confuse_ten_and_twentyfive_gigabit():
    variants = VMWareHandler.get_interface_name_variants("Te1/0/1")

    assert "TwentyFiveGigabitEthernet1/0/1" not in variants


def test_interface_name_variants_keep_names_which_only_start_like_a_short_form():
    # "TenGigE" is not "Te" plus a port number and "Bundle-Ether1" is no Ethernet port at all
    assert VMWareHandler.get_interface_name_variants("TenGigE") == ["TenGigE"]
    assert VMWareHandler.get_interface_name_variants("Bundle-Ether1") == ["Bundle-Ether1"]


# --- reading the neighbor of a physical interface ------------------------------------------------

def test_no_neighbor_reported():
    assert VMWareHandler.get_pnic_neighbor(None) is None
    assert VMWareHandler.get_pnic_neighbor(SimpleNamespace(connectedSwitchPort=None, lldpInfo=None)) is None


def test_cdp_neighbor():
    neighbor = VMWareHandler.get_pnic_neighbor(cdp_hint(system_name=" sw01.example.com ", port_id=" Gi1/0/1 "))

    assert neighbor == {
        "system_name": "sw01.example.com",
        "port_id": "Gi1/0/1",
        "port_description": None,
        "protocol": "CDP"
    }


def test_cdp_neighbor_falls_back_to_the_device_id():
    neighbor = VMWareHandler.get_pnic_neighbor(cdp_hint(system_name="", device_id="sw01", port_id=None))

    assert neighbor.get("system_name") == "sw01"
    assert neighbor.get("port_id") is None


def test_cdp_without_a_name_falls_through_to_lldp():
    hint = lldp_hint(**{"System Name": "sw01", "Port ID": "Gi1/0/1"})
    hint.connectedSwitchPort = SimpleNamespace(systemName=None, devId=None, portId="Gi1/0/1")

    assert VMWareHandler.get_pnic_neighbor(hint).get("protocol") == "LLDP"


def test_lldp_neighbor():
    neighbor = VMWareHandler.get_pnic_neighbor(lldp_hint(**{
        "System Name": "sw01.example.com",
        "Port ID": "XGigabitEthernet0/0/14",
        "Port Description": "MAIN-DETAIL12/Eth1"
    }))

    assert neighbor == {
        "system_name": "sw01.example.com",
        "port_id": "XGigabitEthernet0/0/14",
        "port_description": "MAIN-DETAIL12/Eth1",
        "protocol": "LLDP"
    }


def test_lldp_neighbor_port_id_attribute_is_used_if_no_parameter_was_reported():
    neighbor = VMWareHandler.get_pnic_neighbor(lldp_hint(port_id=42, **{"System Name": "sw01"}))

    assert neighbor.get("port_id") == "42"


def test_lldp_neighbor_without_a_system_name_is_unusable():
    assert VMWareHandler.get_pnic_neighbor(lldp_hint(**{"Port ID": "Gi1/0/1"})) is None


def test_lldp_parameters_which_are_not_a_name_are_ignored():
    hint = lldp_hint(**{"System Name": ["sw01", "sw02"], "Port ID": "Gi1/0/1"})

    assert VMWareHandler.get_pnic_neighbor(hint) is None


# --- cable terminations ---------------------------------------------------------------------------

def test_cable_interface_ids_of_both_sides(inventory, cable_source):
    cable = inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 5, "label": "", "a_terminations": [termination(10)], "b_terminations": [termination(20)]
    })

    assert VMWareHandler.get_cable_interface_ids(cable) == [10, 20]


def test_cable_interface_ids_ignore_terminations_which_are_no_interface(inventory, cable_source):
    cable = inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 5,
        "label": "",
        "a_terminations": [termination(10, object_type="dcim.frontport"), "broken", {"object_id": None}],
        "b_terminations": [termination(20)]
    })

    assert VMWareHandler.get_cable_interface_ids(cable) == [20]


# --- how a cable is named -----------------------------------------------------------------------

def test_a_cable_is_named_after_the_interfaces_it_connects(inventory, cable_source, netbox_interface):
    netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Fa0/16", 200)
    cable = inventory.add_object(NBCable, source=cable_source, data={
        "label": "", "a_terminations": [termination(100)], "b_terminations": [termination(200)]
    })

    assert cable.get_display_name() == "vmnic0 (esx01) <> Fa0/16 (sw01)"


def test_a_cable_read_from_netbox_is_named_after_its_terminations(inventory, cable_source):
    cable = inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 9,
        "label": "",
        "a_terminations": [{"object_type": "dcim.interface", "object_id": 1,
                            "object": {"display": "Gi1/0/2 (sw01)"}}],
        "b_terminations": [{"object_type": "dcim.interface", "object_id": 2,
                            "object": {"display": "vmnic1 (esx02)"}}]
    })

    assert cable.get_display_name() == "Gi1/0/2 (sw01) <> vmnic1 (esx02)"


def test_a_label_someone_set_names_the_cable(inventory, cable_source):
    cable = inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 10, "label": "patch-42", "a_terminations": [termination(1)], "b_terminations": [termination(2)]
    })

    assert cable.get_display_name() == "patch-42"


# --- adding a cable to the reported neighbor ---------------------------------------------------

def test_cable_is_added_between_both_reported_ends(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Fa0/16", 200)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "FastEthernet0/16",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    cables = list(inventory.get_all_items(NBCable))
    assert len(cables) == 1
    assert VMWareHandler.get_cable_interface_ids(cables[0]) == [100, 200]
    assert cables[0].data.get("status") == "connected"
    assert cables[0].source is cable_source


def test_cable_is_added_for_a_port_matched_by_its_description(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "MAIN-DETAIL12/Eth1", 200)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "XGigabitEthernet0/0/14",
                         "port_description": "MAIN-DETAIL12/Eth1", "protocol": "LLDP"}, "esx01", "vmnic0")

    assert len(list(inventory.get_all_items(NBCable))) == 1


def test_a_neighbor_reporting_a_fqdn_matches_the_short_device_name(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Gi1/0/1", 200)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01.example.com", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert len(list(inventory.get_all_items(NBCable))) == 1


def test_an_ambiguous_short_device_name_is_not_matched(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01.dc1.example.com", "Gi1/0/1", 200)
    netbox_interface("sw01.dc2.example.com", "Gi1/0/1", 300)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


def test_two_different_domains_are_two_different_devices(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01.dc1.example.com", "Gi1/0/1", 200)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01.dc2.example.com", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


def test_no_cable_if_the_neighbor_is_not_in_netbox(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


def test_no_cable_if_the_reported_port_is_not_in_netbox(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Gi1/0/2", 200)

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


def test_no_cable_before_both_interfaces_exist_in_netbox(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    switch_interface = netbox_interface("sw01", "Gi1/0/1", 200)
    # a switch port which was discovered during this very run has no NetBox ID yet
    switch_interface.nb_id = 0

    cable_source.add_cable_to_neighbor(
        host_interface, {"system_name": "sw01", "port_id": "Gi1/0/1",
                         "port_description": None, "protocol": "CDP"}, "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


def test_an_existing_cable_is_not_added_a_second_time(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Gi1/0/1", 200)
    inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 7, "label": "", "a_terminations": [termination(200)], "b_terminations": [termination(100)],
        "tags": [{"name": cable_source.source_tag}]
    })
    inventory.resolve_relations()

    neighbor = {"system_name": "sw01", "port_id": "Gi1/0/1", "port_description": None, "protocol": "CDP"}
    cable_source.add_cable_to_neighbor(host_interface, neighbor, "esx01", "vmnic0")

    cables = list(inventory.get_all_items(NBCable))
    assert len(cables) == 1
    # the cable is still reported by this source, so it must not be marked as orphaned
    assert cables[0].source is cable_source


def test_a_cable_created_by_somebody_else_is_not_claimed(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Gi1/0/1", 200)
    inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 7, "label": "", "a_terminations": [termination(100)], "b_terminations": [termination(200)]
    })
    inventory.resolve_relations()

    neighbor = {"system_name": "sw01", "port_id": "Gi1/0/1", "port_description": None, "protocol": "CDP"}
    cable_source.add_cable_to_neighbor(host_interface, neighbor, "esx01", "vmnic0")

    cables = list(inventory.get_all_items(NBCable))
    assert len(cables) == 1
    assert cables[0].source is None


def test_an_interface_which_is_cabled_somewhere_else_is_left_alone(inventory, cable_source, netbox_interface):
    host_interface = netbox_interface("esx01", "vmnic0", 100)
    netbox_interface("sw01", "Gi1/0/1", 200)
    inventory.add_object(NBCable, read_from_netbox=True, data={
        "id": 7, "label": "", "a_terminations": [termination(100)], "b_terminations": [termination(999)]
    })
    inventory.resolve_relations()

    neighbor = {"system_name": "sw01", "port_id": "Gi1/0/1", "port_description": None, "protocol": "CDP"}
    cable_source.add_cable_to_neighbor(host_interface, neighbor, "esx01", "vmnic0")

    assert len(list(inventory.get_all_items(NBCable))) == 1


def test_two_hosts_reporting_each_other_get_one_cable(inventory, cable_source, netbox_interface):
    """A direct link between two ESXi hosts is reported from both sides during the same run."""

    first = netbox_interface("esx01", "vmnic0", 100)
    second = netbox_interface("esx02", "vmnic0", 200)

    cable_source.add_cable_to_neighbor(
        first, {"system_name": "esx02", "port_id": "vmnic0", "port_description": None, "protocol": "LLDP"},
        "esx01", "vmnic0")
    cable_source.add_cable_to_neighbor(
        second, {"system_name": "esx01", "port_id": "vmnic0", "port_description": None, "protocol": "LLDP"},
        "esx02", "vmnic0")

    assert len(list(inventory.get_all_items(NBCable))) == 1


def test_a_pnic_without_an_interface_object_is_skipped(inventory, cable_source, netbox_interface):
    netbox_interface("sw01", "Gi1/0/1", 200)

    cable_source.add_cable_to_neighbor(
        None, {"system_name": "sw01", "port_id": "Gi1/0/1", "port_description": None, "protocol": "CDP"},
        "esx01", "vmnic0")

    assert list(inventory.get_all_items(NBCable)) == []


# --- the option gates the whole feature ---------------------------------------------------------

def test_disabled_by_default(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings)
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    assert sources[0].settings.sync_host_cables is False


def test_nothing_cable_related_happens_while_the_option_is_disabled(vcsim, inventory, load_config,
                                                                    vmware_settings, monkeypatch):
    looked_at = list()
    monkeypatch.setattr(VMWareHandler, "get_pnic_neighbor", staticmethod(looked_at.append))

    load_config(vmware_settings)
    sources = instantiate_sources()
    assert sources and sources[0].init_successful
    source = sources[0]

    # a cable which is not read from NetBox can not be changed, tagged or pruned by this run
    assert NBCable not in source.dependent_netbox_objects

    inventory.resolve_relations()
    source.apply()

    assert looked_at == [], "the CDP/LLDP neighbor of a pNIC must not be read while the option is disabled"
    assert list(inventory.get_all_items(NBCable)) == [], "no cable may be created while the option is disabled"


def test_enabling_the_option_reads_cables_from_netbox(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings + "\nsync_host_cables = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    assert sources[0].settings.sync_host_cables is True
    assert NBCable in sources[0].dependent_netbox_objects


def test_the_option_is_disabled_on_a_netbox_which_is_too_old(vcsim, inventory, load_config, vmware_settings):
    inventory.netbox_api_version = "3.2.0"

    load_config(vmware_settings + "\nsync_host_cables = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    assert sources[0].settings.sync_host_cables is False
    assert NBCable not in sources[0].dependent_netbox_objects


def test_a_sync_with_the_option_enabled_still_works(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings + "\nsync_host_cables = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    inventory.resolve_relations()
    sources[0].apply()

    assert list(inventory.get_all_items(NBDevice)), "hosts must still be synced"
    # none of the captured vcsim inventories reports a CDP/LLDP neighbor
    assert list(inventory.get_all_items(NBCable)) == []
