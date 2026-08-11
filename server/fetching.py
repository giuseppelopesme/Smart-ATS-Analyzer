"""Fetch a document by URL, safely, for a server that stores nothing.

Base64 in the tool call only works when something can produce the base64. A
model that has a PDF attached cannot: faithfully emitting a megabyte of base64
is not something to rely on. Fetching a link is the practical route for a
phone, and it keeps the "nothing is stored" property — the bytes live in
memory for the length of one call.

A server that fetches caller-supplied URLs is an SSRF risk, so this is
deliberately strict:

* HTTPS only.
* Every hostname is resolved and every resulting address checked before
  connecting; private, loopback, link-local, multicast and reserved ranges
  are refused.
* Redirects are followed at most three times, and the destination of each is
  re-validated, because a public host may redirect to 169.254.169.254.
* Responses are capped and read incrementally, so a huge or endless body
  cannot exhaust memory.

Standard library only.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

__all__ = ["fetch_document", "FetchError", "MAX_REDIRECTS"]

MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20
USER_AGENT = "ats-mcp/1.0 (+document validator)"


class FetchError(ValueError):
    """Raised with a message safe to show the caller."""


def _check_address(host: str) -> None:
    """Refuse hosts that resolve anywhere inside the infrastructure."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve host '{host}': {exc}") from exc

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            raise FetchError(f"host '{host}' resolved to an unusable address")
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise FetchError(
                f"refusing to fetch '{host}': it resolves to the non-public "
                f"address {ip}. Only public HTTPS URLs are allowed."
            )


def _validate_url(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FetchError(
            f"only https:// URLs are accepted (got '{parsed.scheme or 'no scheme'}')"
        )
    if not parsed.hostname:
        raise FetchError("URL has no hostname")
    _check_address(parsed.hostname)
    return parsed.hostname, url


def fetch_document(url: str, max_bytes: int) -> Tuple[bytes, str]:
    """Download a document. Returns (content, final_url).

    Raises FetchError with a message that is safe to hand back to the caller.
    """
    current = url
    seen: List[str] = []

    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        seen.append(current)
        request = Request(current, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        })
        context = ssl.create_default_context()
        try:
            # Redirects are handled here rather than by urllib so each hop is
            # re-validated; urllib would follow a redirect to a private address.
            opener = _NoRedirect()
            with opener.open(request, timeout=TIMEOUT_SECONDS, context=context) as resp:
                status = getattr(resp, "status", 200)
                if status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FetchError(f"redirect from {current} had no Location")
                    current = _absolutise(current, location)
                    continue

                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(
                        f"document is {int(declared) // 1024} KB, over the "
                        f"{max_bytes // (1024 * 1024)} MB limit"
                    )

                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(
                            f"document exceeds the {max_bytes // (1024 * 1024)} MB "
                            f"limit (stopped after {total // 1024} KB)"
                        )
                    chunks.append(chunk)

                content = b"".join(chunks)
                if not content:
                    raise FetchError(f"{current} returned an empty body")
                _reject_html_page(content, current)
                return content, current

        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise FetchError("redirect had no Location header") from exc
                current = _absolutise(current, location)
                continue
            raise FetchError(
                f"fetching the document failed with HTTP {exc.code}. "
                f"If this is a share link, check it is public and has not expired."
            ) from exc
        except URLError as exc:
            raise FetchError(f"could not reach the URL: {exc.reason}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise FetchError(f"timed out after {TIMEOUT_SECONDS}s") from exc
        except ssl.SSLError as exc:
            raise FetchError(f"TLS failed: {exc}") from exc

    raise FetchError(
        f"too many redirects (followed {MAX_REDIRECTS}): {' -> '.join(seen[:4])}"
    )


DOC_MAGIC = (
    (b"%PDF", "PDF"),
    (b"PK\x03\x04", "DOCX/ZIP"),
    (b"{\\rtf", "RTF"),
    (b"\xd0\xcf\x11\xe0", "legacy Word"),
)


def _reject_html_page(content: bytes, url: str) -> None:
    """Refuse an HTML landing page delivered in place of a document.

    A share link -- iCloud's especially -- returns a web page with a download
    button, not the file. Fetching it "succeeds", and the analyser would then
    dutifully report on the markup. Failing here with an explanation is far
    better than a confident report about the wrong thing.
    """
    head = content[:1024].lstrip()
    if any(head.startswith(magic) for magic, _ in DOC_MAGIC):
        return
    lowered = head[:512].lower()
    looks_html = (lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html")
                  or b"<head" in lowered or b"<meta" in lowered)
    if not looks_html:
        return  # plain text or markdown is legitimate

    if "icloud.com" in url:
        raise FetchError(
            "that iCloud link returned a web page, not the file. An iCloud "
            "'Copy Link' share URL points at a viewer page whose download runs "
            "in JavaScript, so a server cannot follow it. Either send the file "
            "as base64, or host it somewhere that serves the bytes directly "
            "(a Dropbox link ending in ?dl=1, or a presigned URL)."
        )
    raise FetchError(
        "that URL returned an HTML page rather than a document. It is probably "
        "a share or preview page; use the direct download URL instead."
    )


def _absolutise(base: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, location)


class _NoRedirect:
    """urlopen wrapper that surfaces redirects instead of following them."""

    def open(self, request: Request, timeout: float, context: ssl.SSLContext):
        from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

        class _Stop(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # turns the redirect into an HTTPError we inspect

        opener = build_opener(_Stop(), HTTPSHandler(context=context))
        return opener.open(request, timeout=timeout)


def guess_filename(url: str, fallback: str) -> str:
    """Best-effort filename from a URL path, for choosing a reader."""
    import os
    from urllib.parse import unquote

    path = unquote(urlparse(url).path or "")
    name = os.path.basename(path)
    if name and "." in name:
        return name
    return fallback
