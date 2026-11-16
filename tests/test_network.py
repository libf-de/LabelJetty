"""Network transport: raw-TCP connection, MAC → IP rediscovery via ARP, config.

Uses a throwaway TCP server on 127.0.0.1 standing in for the printer's port 9100
and a fake ``/proc/net/arp`` file — no real printer or LAN access.
"""

import socket
import threading

import pytest

from labeljetty.config import Config
from labeljetty.printer import netdiscovery, network
from labeljetty.printer.network import TSPLPrinterConnectionNetwork
from labeljetty.printer.tspl import TSPLPrinter

MAC = "aa:bb:cc:dd:ee:ff"


class FakePrinterServer:
    """Accepts one connection at a time, records received bytes, and answers
    ``<ESC>!?`` with ``reply`` (optionally preceded by unsolicited ``chatter``)."""

    def __init__(self, reply: bytes = b"\x00", chatter: bytes = b""):
        self.reply = reply
        self.chatter = chatter
        self.received = b""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                if self.chatter:
                    conn.sendall(self.chatter)
                while True:
                    try:
                        data = conn.recv(4096)
                    except OSError:
                        break
                    if not data:
                        break
                    self.received += data
                    if b"\x1b!?" in data:
                        conn.sendall(self.reply)

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    srv = FakePrinterServer()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def isolate_network_state(monkeypatch, tmp_path):
    """Fresh MAC cache per test and an empty fake ARP table by default."""
    network.forget_resolved_hosts()
    arp = tmp_path / "arp"
    arp.write_text(_arp_table({}))
    monkeypatch.setattr(netdiscovery, "ARP_TABLE_PATH", str(arp))
    # Defaults are bound at import; route them through the fake file too.
    monkeypatch.setattr(network, "mac_for_ip", lambda ip: netdiscovery.mac_for_ip(ip, str(arp)))
    yield arp
    network.forget_resolved_hosts()


def _arp_table(entries: dict, flags: str = "0x2") -> str:
    lines = ["IP address       HW type     Flags       HW address            Mask     Device"]
    for ip, mac in entries.items():
        lines.append(f"{ip:<16} 0x1         {flags}         {mac}     *        eth0")
    return "\n".join(lines) + "\n"


def _wait_for(predicate, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- #
#  netdiscovery helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff", "aabbccddeeff"]
)
def test_normalize_mac(raw):
    assert netdiscovery.normalize_mac(raw) == MAC


@pytest.mark.parametrize("raw", ["", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"])
def test_normalize_mac_rejects_garbage(raw):
    with pytest.raises(ValueError):
        netdiscovery.normalize_mac(raw)


def test_read_arp_table_skips_incomplete(tmp_path):
    path = tmp_path / "arp"
    path.write_text(
        _arp_table({"10.0.0.5": "AA:BB:CC:DD:EE:FF"})
        + "10.0.0.9         0x1         0x0         00:00:00:00:00:00     *        eth0\n"
    )
    assert netdiscovery.read_arp_table(str(path)) == {"10.0.0.5": MAC}


def test_read_arp_table_missing_file_is_empty(tmp_path):
    assert netdiscovery.read_arp_table(str(tmp_path / "nope")) == {}


def test_discover_uses_existing_arp_entry_without_probing(monkeypatch, isolate_network_state):
    isolate_network_state.write_text(_arp_table({"10.0.0.7": MAC}))
    probed = []
    monkeypatch.setattr(netdiscovery, "probe_hosts", lambda hosts: probed.extend(hosts))
    assert netdiscovery.discover_ip_for_mac(MAC, path=str(isolate_network_state)) == "10.0.0.7"
    assert probed == []


def test_discover_probes_subnet_then_finds_mac(monkeypatch, isolate_network_state):
    import ipaddress

    arp = isolate_network_state

    def fake_probe(hosts):
        hosts = list(hosts)
        assert "192.168.5.1" in hosts and "192.168.5.254" in hosts
        arp.write_text(_arp_table({"192.168.5.42": MAC}))  # printer answered ARP

    monkeypatch.setattr(netdiscovery, "probe_hosts", fake_probe)
    ip = netdiscovery.discover_ip_for_mac(
        MAC, subnets=[ipaddress.IPv4Network("192.168.5.0/24")], wait=0.5, path=str(arp)
    )
    assert ip == "192.168.5.42"


def test_discover_gives_up_when_mac_absent(monkeypatch, isolate_network_state):
    import ipaddress

    monkeypatch.setattr(netdiscovery, "probe_hosts", lambda hosts: None)
    ip = netdiscovery.discover_ip_for_mac(
        MAC,
        subnets=[ipaddress.IPv4Network("192.168.5.0/30")],
        wait=0.1,
        path=str(isolate_network_state),
    )
    assert ip is None


def test_local_subnets_narrows_large_networks(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        netdiscovery.psutil,
        "net_if_addrs",
        lambda: {
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1", netmask="255.0.0.0")],
            "eth0": [SimpleNamespace(family=socket.AF_INET, address="10.101.3.15", netmask="255.255.0.0")],
            "wlan0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.20", netmask="255.255.255.0")],
        },
    )
    monkeypatch.setattr(
        netdiscovery.psutil,
        "net_if_stats",
        lambda: {n: SimpleNamespace(isup=True) for n in ("lo", "eth0", "wlan0")},
    )
    assert [str(n) for n in netdiscovery.local_subnets()] == ["10.101.3.0/24", "192.168.1.0/24"]


# --------------------------------------------------------------------------- #
#  Connection
# --------------------------------------------------------------------------- #
def test_send_writes_newline_terminated_commands(server):
    con = TSPLPrinterConnectionNetwork(host="127.0.0.1", port=server.port)
    con.connect()
    con.send("CLS")
    con.send_many(["SIZE 57 mm,32 mm", "PRINT 1"])
    con.disconnect()
    assert _wait_for(lambda: server.received == b"CLS\nSIZE 57 mm,32 mm\nPRINT 1\n")


def test_query_returns_status_byte(server):
    server.reply = b"\x04"
    con = TSPLPrinterConnectionNetwork(host="127.0.0.1", port=server.port)
    con.connect()
    assert con.query(b"\x1b!?", raw=True) == b"\x04"
    con.disconnect()


def test_query_drains_unsolicited_chatter_first(server):
    server.chatter = b"SSSGETPRINTING:DONE\r\n"
    con = TSPLPrinterConnectionNetwork(host="127.0.0.1", port=server.port)
    con.connect()
    _wait_for(lambda: False, timeout=0.1)  # let the chatter arrive
    assert con.query(b"\x1b!?", raw=True) == b"\x00"
    con.disconnect()


def test_get_status_over_network_flushes_partial_line(server):
    server.reply = b"Cmd error:\x1b!?\r\n\x00"
    con = TSPLPrinterConnectionNetwork(host="127.0.0.1", port=server.port)
    con.connect()
    printer = TSPLPrinter(connection=con, label_width_mm=57, label_height_mm=32)
    status = printer.get_status()
    con.disconnect()
    assert status.ready and not status.error
    # The bare line ending after <ESC>!? keeps it out of the next command.
    assert _wait_for(lambda: server.received == b"\x1b!?\r\n")


def test_connect_refused_without_mac_raises():
    # A port that was bound but never listened on → connection refused.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    con = TSPLPrinterConnectionNetwork(host="127.0.0.1", port=port, retry_interval=0)
    with pytest.raises(ConnectionError, match=f"127.0.0.1:{port}"):
        con.connect(max_retries=2)


def test_connect_rediscovers_by_mac_and_remembers(monkeypatch, server):
    calls = []

    def fake_discover(mac):
        calls.append(mac)
        return "127.0.0.1"

    monkeypatch.setattr(network, "discover_ip_for_mac", fake_discover)
    # Configured IP is dead (TEST-NET-1, never routable) → rediscover via MAC.
    con = TSPLPrinterConnectionNetwork(
        host="192.0.2.1", port=server.port, mac=MAC, timeout=0.2, retry_interval=0
    )
    con.connect(max_retries=1)
    con.disconnect()
    assert calls == [MAC]
    assert network.remembered_host(MAC) == "127.0.0.1"

    # A fresh connection object (next job) goes straight to the remembered IP.
    con2 = TSPLPrinterConnectionNetwork(host="192.0.2.1", port=server.port, mac=MAC)
    assert con2.target_host() == "127.0.0.1"
    con2.connect(max_retries=1)
    con2.disconnect()
    assert calls == [MAC]
    assert con2.info()["rediscovered"] is True


def test_connect_rejects_ip_now_owned_by_other_device(monkeypatch, server, isolate_network_state):
    isolate_network_state.write_text(_arp_table({"127.0.0.1": "11:22:33:44:55:66"}))
    monkeypatch.setattr(network, "discover_ip_for_mac", lambda mac: None)
    con = TSPLPrinterConnectionNetwork(
        host="127.0.0.1", port=server.port, mac=MAC, retry_interval=0
    )
    with pytest.raises(ConnectionError, match="11:22:33:44:55:66"):
        con.connect(max_retries=1)


def test_connect_with_mac_only_discovers(monkeypatch, server):
    monkeypatch.setattr(network, "discover_ip_for_mac", lambda mac: "127.0.0.1")
    con = TSPLPrinterConnectionNetwork(host=None, port=server.port, mac=MAC)
    con.connect(max_retries=1)
    con.disconnect()
    assert network.remembered_host(MAC) == "127.0.0.1"


def test_needs_host_or_mac():
    with pytest.raises(ValueError):
        TSPLPrinterConnectionNetwork(host=None, mac=None)


# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #
def make_config(**overrides) -> Config:
    base = dict(_env_file=None)
    base.update(overrides)
    return Config(**base)


def test_config_network_connection():
    cfg = make_config(
        PRINTER_CONNECTION="network", PRINTER_HOST="10.101.3.79", PRINTER_MAC="AA-BB-CC-DD-EE-FF"
    )
    con = cfg.get_printer_connection()
    assert isinstance(con, TSPLPrinterConnectionNetwork)
    assert (con.configured_host, con.port, con.mac) == ("10.101.3.79", 9100, MAC)


def test_config_network_requires_host_or_mac():
    with pytest.raises(ValueError, match="PRINTER_HOST"):
        make_config(PRINTER_CONNECTION="network")


def test_config_rejects_bad_mac():
    with pytest.raises(ValueError):
        make_config(PRINTER_MAC="not-a-mac")


def test_config_network_from_env(monkeypatch):
    monkeypatch.setenv("PRINTER_CONNECTION", "network")
    monkeypatch.setenv("PRINTER_HOST", "10.101.3.79")
    monkeypatch.setenv("PRINTER_PORT", "9101")
    con = Config(_env_file=None).get_printer_connection()
    assert (con.configured_host, con.port) == ("10.101.3.79", 9101)


def test_printer_info_endpoint_reports_network(client, monkeypatch):
    from labeljetty.web import api

    monkeypatch.setattr(api.config, "PRINTER_CONNECTION", "network")
    monkeypatch.setattr(
        Config,
        "get_printer_connection",
        lambda self: TSPLPrinterConnectionNetwork(host="10.101.3.79", mac=MAC),
    )
    resp = client.get("/api/printer/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["transport"] == "network"
    assert body["selector"] == "10.101.3.79:9100"
    assert body["mac"] == MAC
    assert body["autodetected"] is False
