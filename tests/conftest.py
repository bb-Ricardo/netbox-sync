"""
Shared fixtures for the netbox-sync test suite.

The integration tests run the real source handlers against vcsim, the vCenter
simulator from the govmomi project, loaded with inventories that were captured
from real vCenters with ``govc object.save`` (see tests/fixtures/vcsim/README.md).
The NetBox side is netbox-sync's own in-memory NetBoxInventory, so no NetBox
instance is needed either.

vcsim is looked up in ``$VCSIM_BIN`` and then on ``$PATH``. Tests that need it are
skipped when it is not installed; unit tests are unaffected.
"""
import os
import shutil
import socket
import ssl
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.config.parser import ConfigParser
from module.netbox.inventory import NetBoxInventory
from module.sources import instantiate_sources

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vcsim"

# every *.tar.gz in the fixture directory is a vcsim inventory; drop a new capture
# there and the vcsim-backed tests pick it up
VCSIM_DUMPS = sorted(p.name[: -len(".tar.gz")] for p in FIXTURE_DIR.glob("*.tar.gz"))

# NetBox version the in-memory inventory pretends to be. 4.2 introduced MAC
# address objects, which is the code path current NetBox releases use.
NETBOX_API_VERSION = "4.3.0"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host: str, port: int, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
            raise RuntimeError(f"vcsim exited with code {process.returncode}: {stderr.strip()}")
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"vcsim did not start listening on {host}:{port} within {timeout}s")


@pytest.fixture(scope="session")
def vcsim_binary() -> str:
    binary = os.environ.get("VCSIM_BIN") or shutil.which("vcsim")
    if binary is None:
        pytest.skip("vcsim not found; set VCSIM_BIN or install it from https://github.com/vmware/govmomi/releases")
    return binary


@pytest.fixture(scope="session", params=VCSIM_DUMPS, ids=VCSIM_DUMPS)
def vcsim(request, vcsim_binary, tmp_path_factory):
    """
    A vcsim process serving one captured inventory. Session scoped, so each dump
    is started once per test run; parametrized, so every vcsim-backed test runs
    against every dump.
    """
    name = request.param
    extract_dir = tmp_path_factory.mktemp("vcsim")
    with tarfile.open(FIXTURE_DIR / f"{name}.tar.gz") as archive:
        archive.extractall(extract_dir, filter="data")
    # govc object.save writes into a directory named after the vCenter
    load_dir = next(p for p in extract_dir.iterdir() if p.is_dir())

    host, port = "127.0.0.1", _free_port()
    process = subprocess.Popen(
        [vcsim_binary, "-load", str(load_dir), "-l", f"{host}:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(host, port, process)
        # vcsim accepts any credentials
        yield SimpleNamespace(name=name, host=host, port=port, username="user", password="pass")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def inventory():
    """
    A fresh in-memory NetBoxInventory. The class is a singleton with class-level
    state, so it is reset before and after each test.
    """
    def _reset():
        inv = NetBoxInventory()
        inv.base_structure = {}
        inv.source_list = []
        inv.init()
        inv.netbox_api_version = NETBOX_API_VERSION
        return inv

    inv = _reset()
    yield inv
    _reset()


@pytest.fixture
def load_config(tmp_path):
    """
    Returns a function that feeds a settings.ini text to netbox-sync's ConfigParser
    singleton, replacing whatever a previous test loaded.
    """
    def _load(text: str) -> ConfigParser:
        config_file = tmp_path / "settings.ini"
        config_file.write_text(text)
        parser = ConfigParser()
        parser.file_list.clear()
        parser.content.clear()
        parser.config_errors.clear()
        parser.config_warnings.clear()
        parser.parsing_finished = False
        parser.add_config_file(str(config_file))
        parser.read_config()
        return parser

    return _load


@pytest.fixture
def vmware_settings(vcsim) -> str:
    """settings.ini pointing the VMware source at the running vcsim."""
    return f"""
[netbox]
api_token = not-used-by-these-tests
host_fqdn = 127.0.0.1

[source/{vcsim.name}]
type = vmware
host_fqdn = {vcsim.host}
port = {vcsim.port}
username = {vcsim.username}
password = {vcsim.password}
validate_tls_certs = False
permitted_subnets = 0.0.0.0/0, ::/0
dns_name_lookup = False
vm_disk_and_ram_in_decimal = False
"""


@pytest.fixture
def vmware_source(vcsim, inventory, load_config, vmware_settings):
    """The instantiated VMware source handler for the running vcsim, not yet applied."""
    load_config(vmware_settings)
    sources = instantiate_sources()
    assert len(sources) == 1 and sources[0].init_successful, "VMware source failed to initialise"
    inventory.resolve_relations()
    return sources[0]


@pytest.fixture
def vmware_sync(inventory, vmware_source):
    """The inventory after one full VMware source run, plus the source that produced it."""
    vmware_source.apply()
    return SimpleNamespace(inventory=inventory, source=vmware_source)


@pytest.fixture
def sdk(vcsim):
    """
    A pyVmomi ServiceContent for the running vcsim, independent of netbox-sync, so
    tests can compare what was synced with what the SDK reports.
    """
    from pyVim import connect

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    instance = connect.SmartConnect(
        host=vcsim.host, port=vcsim.port, user=vcsim.username, pwd=vcsim.password, sslContext=context,
    )
    try:
        yield instance.RetrieveContent()
    finally:
        connect.Disconnect(instance)
