from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .errors import CrawlerError, ErrorCode


_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class PublicHttpsUrlPolicy:
    """Allowlisted HTTPS hosts only, with a best-effort SSRF DNS check."""

    def validate(self, url: str, allowed_domains: frozenset[str]) -> None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise CrawlerError(ErrorCode.UNSAFE_URL, "only public HTTPS URLs are allowed")
        if not any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
            raise CrawlerError(ErrorCode.PERMISSION_DENIED, f"domain {host!r} is not allowed")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise CrawlerError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                "could not resolve media host",
                retryable=True,
            ) from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # Clash-style proxy DNS uses RFC 2544 benchmark addresses as
            # synthetic public-host placeholders. The hostname has already
            # passed the strict domain allowlist above, so accept only this
            # dedicated fake-IP range while continuing to reject real LAN,
            # loopback, link-local, and other reserved destinations.
            if not ip.is_global and ip not in _PROXY_FAKE_IP_NETWORK:
                raise CrawlerError(ErrorCode.UNSAFE_URL, "media URL resolved to a non-public address")

