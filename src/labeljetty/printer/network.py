"""Raw-TCP (port 9100, "JetDirect") connection to a networked TSPL printer.

Drop-in alternative to :class:`TSPLPrinterConnectionUSB`: same ``connect`` /
``disconnect`` / ``send`` / ``receive`` / ``query`` surface, so ``TSPLPrinter``,
the worker and the status endpoints don't care which transport is in use.

Optionally pinned to the printer's MAC address: if the printer isn't reachable
at its configured/last-known IP — or that IP now belongs to a different device —
the IP is looked up again via ARP (see :mod:`labeljetty.printer.netdiscovery`).
A discovered IP is remembered for the lifetime of the process.
"""

import socket
import time
from typing import Dict, List, Optional, Self, Union

from labeljetty.printer.connection import TSPLPrinterConnectionUSB
from labeljetty.printer.netdiscovery import (
    discover_ip_for_mac,
    mac_for_ip,
    normalize_mac,
)

# MAC → IP found by ARP discovery (or verified on connect). Module-level so it
# outlives the per-job connection objects; each process keeps its own copy.
_resolved_hosts: Dict[str, str] = {}


def remembered_host(mac: str) -> Optional[str]:
    """The IP last resolved for ``mac`` in this process, if any."""
    return _resolved_hosts.get(normalize_mac(mac))


def forget_resolved_hosts() -> None:
    """Drop every remembered MAC → IP mapping (used by tests)."""
    _resolved_hosts.clear()


class TSPLPrinterConnectionNetwork:
    """TSPL over a plain TCP socket (one connection per ``connect`` call)."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: int = 9100,
        mac: Optional[str] = None,
        timeout: float = 5.0,
        retry_interval: float = 1.0,
    ) -> None:
        if not host and not mac:
            raise ValueError("A network printer needs a host and/or a MAC address.")
        self.configured_host: Optional[str] = host
        self.port: int = port
        self.mac: Optional[str] = normalize_mac(mac) if mac else None
        self.timeout: float = timeout
        self.retry_interval: float = retry_interval
        self.sock: Optional[socket.socket] = None
        # Address of the current/last successful connection.
        self.host: Optional[str] = None

    # ---------------------------------------------------------
    #  Target resolution
    # ---------------------------------------------------------
    def target_host(self) -> Optional[str]:
        """Where to connect: the IP remembered for our MAC, else the configured host."""
        if self.mac and self.mac in _resolved_hosts:
            return _resolved_hosts[self.mac]
        return self.configured_host

    def _rediscover(self) -> Optional[str]:
        """Look the printer's IP up again by MAC; remember and return it."""
        print(f"Looking up network printer {self.mac} via ARP...")
        ip = discover_ip_for_mac(self.mac)
        if ip:
            print(f"Found network printer {self.mac} at {ip}.")
            _resolved_hosts[self.mac] = ip
        else:
            print(f"Network printer {self.mac} not found on the local network.")
        return ip

    def _open(self, host: str) -> None:
        """Open the TCP connection and, with a MAC configured, make sure the peer
        really is our printer (the IP may have been handed to another device)."""
        sock = socket.create_connection((host, self.port), timeout=self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer_ip = sock.getpeername()[0]
            if self.mac:
                # Only verifiable on the local segment; through a router the ARP
                # table has no entry for the peer and we trust the connection.
                seen_mac = mac_for_ip(peer_ip)
                if seen_mac and seen_mac != self.mac:
                    raise ConnectionError(
                        f"{peer_ip} belongs to {seen_mac}, not the printer {self.mac}"
                    )
                _resolved_hosts[self.mac] = peer_ip
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.host = host

    # ---------------------------------------------------------
    #  Connect / disconnect
    # ---------------------------------------------------------
    def connect(self, max_retries: int = 5) -> bool:
        """Connect to the printer, retrying on transient network errors.

        With a MAC configured, the first failure triggers one ARP lookup; if the
        printer turns up at a different IP we switch to it right away."""
        self.disconnect()
        attempt = 0
        rediscovered = False
        while True:
            host = self.target_host()
            try:
                if not host:
                    raise ConnectionError(
                        f"IP address of network printer {self.mac} is unknown"
                    )
                self._open(host)
                print(f"Connected to TSPL printer at {host}:{self.port}.")
                return True
            except OSError as e:
                if self.mac and not rediscovered:
                    rediscovered = True
                    new_host = self._rediscover()
                    if new_host and new_host != host:
                        continue  # retry right away at the new address
                attempt += 1
                if attempt >= max_retries:
                    target = f"{host}:{self.port}" if host else self.mac
                    raise ConnectionError(
                        f"Could not connect to network printer {target}: {e}"
                    ) from e
                print(f"Connection error: {e}. Retrying ({attempt}/{max_retries})...")
                time.sleep(self.retry_interval)

    def disconnect(self) -> None:
        """Close the socket. Idempotent."""
        if self.sock is None:
            return
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def info(self) -> dict:
        """Connection facts for the UI and the ``/printer/info`` endpoint.

        Never touches the network (beyond the in-process cache)."""
        host = self.target_host()
        return {
            "transport": "network",
            "selector": f"{host or '?'}:{self.port}",
            "host": host,
            "port": self.port,
            "mac": self.mac,
            "configured_host": self.configured_host,
            "rediscovered": bool(host and host != self.configured_host),
            "description": f"Network printer at {host or 'unknown IP'}:{self.port}",
        }

    # ---------------------------------------------------------
    #  I/O
    # ---------------------------------------------------------
    _to_wire = staticmethod(TSPLPrinterConnectionUSB._to_wire)

    def send(self, cmd: str | bytes, raw: bool = False) -> None:
        if self.sock is None:
            self.connect()

        data = self._to_wire(cmd, raw)

        try:
            self.sock.sendall(data)
        except OSError:
            print("Network write failed — reconnecting...")
            self.connect()
            self.sock.sendall(data)

    def send_many(
        self, commands: Union[List[str], tuple[str, ...]], raw: bool = False
    ) -> None:
        for c in commands:
            self.send(c, raw=raw)

    def receive(self, timeout: int = 1000, max_length: int = 1024) -> Optional[bytes]:
        """Read whatever the printer sent, waiting up to ``timeout`` ms for the
        first byte. Returns ``None`` on timeout or a closed connection."""
        if self.sock is None:
            raise RuntimeError("Not connected to network printer. Call connect() first.")
        try:
            self.sock.settimeout(timeout / 1000)
            data = self.sock.recv(max_length)
        except socket.timeout:
            return None
        except OSError as e:
            print(f"Network read failed: {e}")
            self.disconnect()
            return None
        finally:
            if self.sock is not None:
                self.sock.settimeout(self.timeout)
        if not data:
            # Peer closed the connection.
            self.disconnect()
            return None
        return data

    def drain(self) -> bytes:
        """Discard (and return) unsolicited output already waiting on the socket,
        e.g. ``SSSGETPRINTING:DONE`` progress lines or ``Cmd error:…`` replies, so
        it isn't mistaken for the answer to the next query."""
        pending = b""
        while self.sock is not None:
            chunk = self.receive(timeout=10)
            if not chunk:
                break
            pending += chunk
        if pending:
            print(f"Printer said: {pending!r}")
        return pending

    def receive_string(
        self, timeout: int = 1000, max_length: int = 1024, encoding: str = "ascii"
    ) -> Optional[str]:
        data = self.receive(timeout=timeout, max_length=max_length)
        if data is None:
            return None
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError as e:
            print(f"Decode error: {e}")
            return None

    def query(
        self, cmd: str | bytes, timeout: int = 1000, max_length: int = 1024, raw: bool = False
    ) -> Optional[bytes]:
        if self.sock is None:
            self.connect()
        self.drain()
        self.send(cmd, raw=raw)
        time.sleep(0.1)  # Brief delay for printer to process
        return self.receive(timeout=timeout, max_length=max_length)

    def query_string(
        self, cmd: str, timeout: int = 1000, max_length: int = 1024
    ) -> Optional[str]:
        if self.sock is None:
            self.connect()
        self.drain()
        self.send(cmd)
        time.sleep(0.1)  # Brief delay for printer to process
        return self.receive_string(timeout=timeout, max_length=max_length)
