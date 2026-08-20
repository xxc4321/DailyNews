from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .extract import read_bounded


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedUrl:
    url: str
    hostname: str
    addresses: tuple[str, ...]


Resolver = Callable[..., list]


def validate_public_url(url: str, resolver: Resolver = socket.getaddrinfo) -> ParsedUrl:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("only public HTTP(S) URLs are allowed")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("credential-free hostname is required")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("localhost is not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        rows = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as exc:
        raise UnsafeUrlError("hostname could not be resolved safely") from exc
    addresses = tuple(sorted({row[4][0].split("%", 1)[0] for row in rows}))
    if not addresses:
        raise UnsafeUrlError("hostname resolved to no addresses")
    try:
        if any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise UnsafeUrlError("URL resolves to a non-public address")
    except ValueError as exc:
        raise UnsafeUrlError("resolver returned an invalid address") from exc
    return ParsedUrl(url=parsed.geturl(), hostname=hostname, addresses=addresses)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class FetchResult:
    url: str
    body: bytes
    headers: dict[str, str]
    status: int


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 5,
        resolver: Resolver = socket.getaddrinfo,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver
        self._opener = build_opener(_NoRedirect())

    def fetch(self, url: str) -> FetchResult:
        current = url
        for redirect_count in range(self.max_redirects + 1):
            validate_public_url(current, resolver=self.resolver)
            request = Request(
                current,
                headers={
                    "User-Agent": "JNBY-News-Watch/0.1 (+local research; read-only)",
                    "Accept": "text/html,application/rss+xml,application/atom+xml,application/json;q=0.9,*/*;q=0.5",
                },
            )
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location or redirect_count >= self.max_redirects:
                        raise UnsafeUrlError("redirect chain is invalid or too long") from exc
                    current = urljoin(current, location)
                    continue
                raise
            with response:
                final_url = response.geturl()
                validate_public_url(final_url, resolver=self.resolver)
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_length = headers.get("content-length")
                if content_length and int(content_length) > self.max_bytes:
                    raise ValueError("response exceeds configured byte limit")
                body = read_bounded(
                    iter(lambda: response.read(64 * 1024), b""), max_bytes=self.max_bytes
                )
                return FetchResult(
                    url=final_url,
                    body=body,
                    headers=headers,
                    status=getattr(response, "status", 200),
                )
        raise UnsafeUrlError("redirect chain exceeded configured limit")
