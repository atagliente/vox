"""
How announcements travel: UDP multicast, not sniffing.

Why not promiscuous mode:
  - it needs CAP_NET_RAW, which in practice means root;
  - on a switched network other people's unicast traffic never reaches your NIC;
  - inside a cloud VPC, L2 broadcast is disabled anyway.
The only thing you would really see is the multicast and broadcast traffic —
exactly what is wanted here. An ordinary socket receives it just as well.

A limit worth knowing: multicast does not cross routers by default. Beyond a
single L2 segment (or in the cloud) a fallback is needed: a static seed list,
or a registry.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import sys

DEFAULT_GROUP = "239.17.42.1"  # the administratively-scoped range
DEFAULT_PORT = 45177


def make_sender(
    group: str = DEFAULT_GROUP, ttl: int = 1, interface: str | None = None
) -> socket.socket:
    """The sending socket.

    ttl=1 confines announcements to the local segment, which is the careful
    default. Raising it exposes the agents' presence beyond the subnet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", ttl))
    # Needed so other processes on this same host see our own packets.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    if interface:
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
        )
    return sock


def make_receiver(
    group: str = DEFAULT_GROUP,
    port: int = DEFAULT_PORT,
    interface: str = "0.0.0.0",  # nosec B104 - see the docstring
) -> socket.socket:
    """The receiving socket, joined to the multicast group.

    Bound to every interface on purpose, and not a slip: a multicast group is
    joined on an interface, and which one a peer will announce on is not
    something this can know in advance. The traffic does not leave the
    segment, and nothing is trusted for having arrived on it — the mTLS
    handshake in whois.py is what decides who a peer is.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
        # Lets several agents on one host listen on the same port.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(interface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(1.0)  # a short timeout, so the thread can shut down cleanly
    return sock


def local_address(probe_target: str = "8.8.8.8") -> str:
    """The local address used to reach the network.

    No packet is sent: connect() on a UDP socket only selects the route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((probe_target, 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
