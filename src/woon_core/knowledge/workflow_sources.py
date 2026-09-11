"""Bounded explicit inputs for the standalone knowledge workflow."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import threading
from contextlib import suppress
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.document_intake import ingest_document_candidate

MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 60_000


def snapshot_source(
    request: dict[str, str],
    directory: Path,
    *,
    model_cache: Path | None = None,
) -> dict[str, str]:
    """Save raw bytes and extracted text in a job; never accept or compile knowledge."""
    if not isinstance(request, dict) or len(request) != 1:
        raise WoonError("source requires exactly one of text, path, or url")
    kind, value = next(iter(request.items()))
    if kind not in {"text", "path", "url"} or not isinstance(value, str) or not value.strip():
        raise WoonError("source requires non-empty text, path, or url")
    suffix, locator = ".txt", "provided-text"
    if kind == "text":
        raw = value.encode("utf-8")
    elif kind == "path":
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_file():
            raise WoonError("source must be an existing regular file, not a symlink")
        suffix, locator = path.suffix.lower(), path.name
        if suffix not in {".md", ".txt", ".pdf"}:
            raise WoonError("local sources must be Markdown, text, or PDF")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
    else:
        raw, content_type, locator = fetch_public_url(value)
        suffix = ".pdf" if content_type == "application/pdf" else ".html"
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise WoonError("source is empty or exceeds the 10 MiB limit")
    raw_path = directory / ("raw-source" + suffix)
    atomic_write(raw_path, raw, mode=0o600)
    conversion_receipt = ""
    if suffix == ".pdf":
        converted = ingest_document_candidate(
            raw_path,
            directory,
            model_cache=model_cache,
            source_locator="source.pdf",
        )
        receipt_path = directory / converted.receipt
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["conversion_status"] != "success" or receipt["conversion_errors"]:
            raise WoonError("PDF extraction was incomplete; automatic application is held")
        candidate = directory / str(converted.candidate)
        text = (candidate / "document.raw.md").read_text(encoding="utf-8")
        conversion_receipt = converted.receipt
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as error:
            raise WoonError("text source must be UTF-8") from error
        if suffix == ".html":
            parser = _PublicHTML(locator)
            parser.feed(text)
            text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines())
    if not text.strip() or len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise WoonError("extracted source is empty, invalid, or exceeds 60,000 characters")
    atomic_write(directory / "source.txt", text.encode("utf-8"), mode=0o600)
    return {
        "locator": locator,
        "raw_file": raw_path.name,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "conversion_receipt": conversion_receipt,
    }


def fetch_public_url(url: str) -> tuple[bytes, str, str]:
    """Fetch at most four public HTTP(S) hops, connecting only to checked IP addresses."""
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise WoonError("source URL must be public HTTP(S) without credentials")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if port not in {80, 443}:
                raise WoonError("source URL must use port 80 or 443")
            addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(address[4][0]).is_global for address in addresses
            ):
                raise WoonError("source URL resolves to a non-public address")
            # Pin the connection to a validated IP so a second DNS answer cannot rebind it.
            sock = socket.create_connection((str(addresses[0][4][0]), port), timeout=20)
            if parsed.scheme == "https":
                try:
                    sock = ssl.create_default_context().wrap_socket(
                        sock,
                        server_hostname=parsed.hostname,
                    )
                except BaseException:
                    sock.close()
                    raise
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=20)
            connection.sock = sock
            deadline = threading.Timer(60, _close_slow_connection, args=(sock,))
            deadline.daemon = True
            deadline.start()
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Accept": "text/html,application/pdf",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Woon-Knowledge/0.1",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise WoonError("source URL returned a redirect without a location")
                    redirected = urljoin(url, location)
                    if parsed.scheme == "https" and urlsplit(redirected).scheme != "https":
                        raise WoonError("source URL redirects from HTTPS to an insecure address")
                    url = redirected
                    continue
                if response.status != 200:
                    raise WoonError(f"source URL returned HTTP {response.status}")
                if response.getheader("Content-Encoding", "identity") != "identity":
                    raise WoonError("source URL requires an unsupported content encoding")
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in {"text/html", "application/pdf"}:
                    raise WoonError("source URL must return HTML or PDF")
                data = response.read(MAX_SOURCE_BYTES + 1)
                if not data or len(data) > MAX_SOURCE_BYTES:
                    raise WoonError("source URL is empty or exceeds the 10 MiB limit")
                return data, content_type, url
            finally:
                deadline.cancel()
                connection.close()
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise WoonError(f"public source fetch failed: {type(error).__name__}") from error
    raise WoonError("source URL exceeded three redirects")


def _close_slow_connection(sock: socket.socket) -> None:
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    sock.close()


class _PublicHTML(HTMLParser):
    """Extract visible HTML text and link destinations without running browser code."""

    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "form"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                link = urljoin(self.url, href)
                if urlsplit(link).scheme in {"http", "https"}:
                    self.parts.append(f" <{link}> ")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)
