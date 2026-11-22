"""SSRF-safe URL handling for audio_url and callback_url.

Rules: http(s) only; every resolved address must be globally routable (no
private, loopback, link-local, multicast, reserved, or cloud-metadata ranges);
redirects are followed manually and each hop is re-validated; size and time are
bounded. Residual risk (documented): DNS can change between our check and the
connect (rebinding); the short window and per-hop checks keep this small.
"""
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.core.errors import AppError, ErrorCode

_resolver = socket.getaddrinfo  # patched in tests


def _addr_ok(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_loopback or a.is_link_local or a.is_private)


def check_public_url(url: str, allowed_hosts: list[str] | None = None) -> str:
    p = urlparse(str(url))
    if p.scheme not in ("http", "https") or not p.hostname:
        raise AppError(ErrorCode.UNSAFE_URL, details={"reason": "scheme must be http or https"})
    if p.username or p.password:
        raise AppError(ErrorCode.UNSAFE_URL, details={"reason": "credentials in URL are not allowed"})
    host = p.hostname.lower()
    if allowed_hosts and host not in allowed_hosts:
        raise AppError(ErrorCode.UNSAFE_URL, details={"reason": "host not in allowlist", "host": host})
    try:
        infos = _resolver(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise AppError(ErrorCode.UNSAFE_URL, details={"reason": "host does not resolve", "host": host}) from None
    ips = {i[4][0] for i in infos}
    bad = [ip for ip in ips if not _addr_ok(ip)]
    if not ips or bad:
        raise AppError(ErrorCode.UNSAFE_URL, details={"reason": "host resolves to a non-public address", "host": host})
    return host


def safe_download(url: str, dest: Path, max_bytes: int, timeout_s: float, max_redirects: int = 5,
                  transport: httpx.BaseTransport | None = None) -> tuple[Path, str | None]:
    """Download to dest. Returns (path, content_type)."""
    current = str(url)
    with httpx.Client(timeout=timeout_s, follow_redirects=False, transport=transport) as client:
        for _ in range(max_redirects + 1):
            check_public_url(current)
            try:
                with client.stream("GET", current) as r:
                    if r.is_redirect:
                        current = urljoin(current, r.headers.get("location", ""))
                        continue
                    if r.status_code >= 400:
                        raise AppError(ErrorCode.INPUT_DOWNLOAD_FAILED, retryable=r.status_code >= 500,
                                       details={"status": r.status_code})
                    declared = int(r.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise AppError(ErrorCode.FILE_TOO_LARGE, details={"limit_bytes": max_bytes})
                    size = 0
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            size += len(chunk)
                            if size > max_bytes:
                                raise AppError(ErrorCode.FILE_TOO_LARGE, details={"limit_bytes": max_bytes})
                            f.write(chunk)
                    return dest, r.headers.get("content-type")
            except httpx.HTTPError as exc:
                raise AppError(ErrorCode.INPUT_DOWNLOAD_FAILED, details={"reason": type(exc).__name__}) from exc
    raise AppError(ErrorCode.INPUT_DOWNLOAD_FAILED, details={"reason": "too many redirects"})
