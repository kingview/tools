from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .errors import CrawlerError, ErrorCode


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
            if not ip.is_global:
                raise CrawlerError(ErrorCode.UNSAFE_URL, "media URL resolved to a non-public address")

