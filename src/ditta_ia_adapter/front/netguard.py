"""Decide whether an announced endpoint is safe to connect to.

Announced endpoints are chosen by whoever signed the announcement, and keys
are free. Connecting to one blindly would let a stranger aim this node at
its own network (loopback, private ranges, cloud metadata addresses), so
outbound connections are refused there unless configuration allows it
(spec, Security considerations).
"""

import ipaddress
import socket
from urllib.parse import urlsplit


def endpoint_allowed(endpoint: str, allow_private: bool = False) -> bool:
    """True if posting to this base URL is permitted."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname
    if not host or parts.username or parts.password:
        return False
    # Onion endpoints need a Tor proxy, which this adapter does not run.
    if host.endswith(".onion"):
        return False
    # Plain HTTP is only acceptable on a link the operator controls.
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        return False
    if allow_private:
        return True
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(_is_public(address) for address in addresses)


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_global
    except ValueError:
        return False
