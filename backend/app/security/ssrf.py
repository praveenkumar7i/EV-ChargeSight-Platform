"""Outbound URL checks for operator API connections (server-side request forgery protection).

Rules: https only (http only when private destinations are explicitly allowed for an
administrator-controlled deployment), no credentials in the URL, and every address the host name
resolves to must be public (not loopback, private, link-local, multicast, reserved or unspecified).
Redirects are never followed by the connector.

Limitation (documented): the HTTP client resolves the name again when connecting, so a DNS-rebinding
attacker controlling a domain's DNS could still change the address between check and connect.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    pass


def _is_public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or (isinstance(a, ipaddress.IPv4Address) and a in ipaddress.ip_network("100.64.0.0/10")))


def check_url(url: str, *, allow_private: bool = False, resolver=socket.getaddrinfo) -> str:
    """Validate a base URL. Returns the normalised URL or raises UnsafeUrl with a user-facing reason."""
    url = (url or "").strip()
    if len(url) > 300:
        raise UnsafeUrl("The URL is too long.")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise UnsafeUrl("The base URL must start with https://.")
    if parts.scheme == "http" and not allow_private:
        raise UnsafeUrl("Plain http:// is not allowed; the operator API must use https://.")
    if parts.username or parts.password:
        raise UnsafeUrl("Do not put credentials in the URL; use the credential field.")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("The URL has no host name.")
    if parts.query or parts.fragment:
        raise UnsafeUrl("The base URL must not contain a query string or fragment.")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeUrl("The URL port is invalid.")
    if allow_private:
        return url.rstrip("/")
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost") \
            or host.lower().endswith(".local") or host.lower().endswith(".internal"):
        raise UnsafeUrl("Local or internal host names are not allowed.")
    try:
        infos = resolver(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise UnsafeUrl(f"The host name '{host}' could not be resolved.")
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise UnsafeUrl(f"The host name '{host}' did not resolve to any address.")
    bad = [a for a in addrs if not _is_public(a.split("%")[0])]
    if bad:
        raise UnsafeUrl("The host resolves to a private, loopback or otherwise non-public address, which is not allowed.")
    return url.rstrip("/")
