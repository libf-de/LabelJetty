"""Find a network printer's IP address from its MAC address via ARP (Linux).

DHCP may hand a printer a new IP; its MAC stays the same. The kernel's ARP
(neighbour) table maps IPs to MACs, but only for hosts we recently talked to —
so a lookup first checks the table and, failing that, nudges every address of
the local IPv4 subnet(s) with a tiny UDP datagram (each send forces an ARP
request) and polls the table for the MAC.

Needs no root and no extra tools: ``/proc/net/arp`` is world-readable and the
probe is an ordinary unprivileged UDP send. It only sees the layer-2 segment
this process is attached to — inside Docker that means ``network_mode: host``.
"""

import ipaddress
import re
import socket
import time
from typing import Dict, Iterable, List, Optional

import psutil

ARP_TABLE_PATH = "/proc/net/arp"

# /proc/net/arp "Flags" column: ATF_COM (0x2) = completed entry (MAC known).
_ATF_COM = 0x2

# Subnets larger than this are not swept exhaustively — only the /24 around our
# own address. Keeps a discovery to ~254 probes (and well under the kernel's
# neighbour-table limits) on oversized networks.
_MAX_SWEEP_PREFIX = 22

# UDP "discard" port: anything works, the datagram only exists to trigger ARP.
_PROBE_PORT = 9

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(mac: str) -> str:
    """Return ``mac`` as lowercase ``aa:bb:cc:dd:ee:ff``.

    Accepts ``:``, ``-`` or ``.`` separators (or none). Raises ``ValueError`` for
    anything that isn't 6 bytes of hex."""
    digits = re.sub(r"[:\-.\s]", "", mac.strip().lower())
    if not _MAC_RE.match(digits):
        raise ValueError(f"Invalid MAC address: {mac!r} (expected e.g. aa:bb:cc:dd:ee:ff)")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def read_arp_table(path: str = ARP_TABLE_PATH) -> Dict[str, str]:
    """``{ip: mac}`` for every completed entry in the kernel ARP table.

    Returns ``{}`` when the table can't be read (non-Linux, sandbox)."""
    try:
        with open(path, encoding="ascii") as f:
            lines = f.read().splitlines()[1:]  # skip header
    except OSError:
        return {}
    table: Dict[str, str] = {}
    for line in lines:
        cols = line.split()
        # IP address, HW type, Flags, HW address, Mask, Device
        if len(cols) < 4:
            continue
        ip, flags, mac = cols[0], cols[2], cols[3]
        try:
            if not int(flags, 16) & _ATF_COM:
                continue
            table[ip] = normalize_mac(mac)
        except ValueError:
            continue
    return table


def mac_for_ip(ip: str, path: str = ARP_TABLE_PATH) -> Optional[str]:
    """The MAC the ARP table currently holds for ``ip``, or ``None`` if unknown
    (e.g. never contacted, or reached through a router)."""
    return read_arp_table(path).get(ip)


def find_ip_in_arp_table(mac: str, path: str = ARP_TABLE_PATH) -> Optional[str]:
    """The IP currently mapped to ``mac`` in the ARP table, or ``None``."""
    mac = normalize_mac(mac)
    for ip, entry_mac in read_arp_table(path).items():
        if entry_mac == mac:
            return ip
    return None


def local_subnets() -> List[ipaddress.IPv4Network]:
    """IPv4 networks of the host's non-loopback interfaces that are up.

    Oversized networks (larger than /22) are narrowed to the /24 around our own
    address so a sweep stays small."""
    nets: List[ipaddress.IPv4Network] = []
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
    except Exception:
        return nets
    for iface, entries in addrs.items():
        if iface in stats and not stats[iface].isup:
            continue
        for entry in entries:
            if entry.family != socket.AF_INET or not entry.netmask:
                continue
            try:
                iface_ip = ipaddress.IPv4Interface(f"{entry.address}/{entry.netmask}")
            except ValueError:
                continue
            if iface_ip.ip.is_loopback or iface_ip.ip.is_link_local:
                continue
            net = iface_ip.network
            if net.prefixlen < _MAX_SWEEP_PREFIX:
                net = ipaddress.IPv4Interface(f"{entry.address}/24").network
            if net.num_addresses <= 2 or net in nets:
                continue  # /31-/32 point-to-point: nothing to sweep
            nets.append(net)
    return nets


def probe_hosts(hosts: Iterable[str]) -> None:
    """Send one empty UDP datagram to each host so the kernel ARPs for it.

    Fire-and-forget: errors (unreachable, permission, buffer full) are ignored —
    the ARP request goes out regardless of whether anything listens."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        for host in hosts:
            try:
                sock.sendto(b"", (host, _PROBE_PORT))
            except OSError:
                pass
    finally:
        sock.close()


def discover_ip_for_mac(
    mac: str,
    subnets: Optional[List[ipaddress.IPv4Network]] = None,
    wait: float = 3.0,
    poll_interval: float = 0.25,
    path: str = ARP_TABLE_PATH,
) -> Optional[str]:
    """Find the IP that currently answers ARP for ``mac``.

    Checks the ARP table first (free); otherwise probes every address of
    ``subnets`` (default: the host's local subnets) and polls the table for up
    to ``wait`` seconds. Returns ``None`` if the MAC never shows up."""
    mac = normalize_mac(mac)
    ip = find_ip_in_arp_table(mac, path)
    if ip:
        return ip

    if subnets is None:
        subnets = local_subnets()
    hosts = [str(h) for net in subnets for h in net.hosts()]
    if not hosts:
        return None
    probe_hosts(hosts)

    deadline = time.monotonic() + wait
    while True:
        ip = find_ip_in_arp_table(mac, path)
        if ip or time.monotonic() >= deadline:
            return ip
        time.sleep(poll_interval)
