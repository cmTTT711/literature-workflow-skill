"""Anonymous, serial public downloads; no browser cookies or institutional credentials."""

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import httpx

from ..config import credential
from ..locking import RunLock
from ..storage import write_new
from .models import AcquisitionError, AcquisitionOptions

AGENT = "Paper-Acquisition/0.2"


class ContentAuth(httpx.Auth):
    """Authenticate only OpenAlex's documented per-work PDF endpoint."""

    def __init__(self, key):
        self.key = key

    def auth_flow(self, request):
        if (
            self.key
            and request.url.scheme == "https"
            and request.url.host == "content.openalex.org"
            and re.fullmatch(r"/works/W\d+\.pdf", request.url.path)
        ):
            request.url = request.url.copy_set_param("api_key", self.key)
        response = yield request
        # Redirects are recorded by the downloader; never persist an API key there.
        if location := response.headers.get("location"):
            parts = urlsplit(location)
            if any(k.lower() in {"api_key", "apikey"} for k, _ in parse_qsl(parts.query)):
                response.headers["location"] = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        parts.path,
                        urlencode(
                            [
                                (k, v)
                                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                                if k.lower() not in {"api_key", "apikey"}
                            ]
                        ),
                        parts.fragment,
                    )
                )


# Some local TUN proxies synthesize this benchmarking range. Exceptions are exact
# public scholarly hostnames over certificate-validated HTTPS, never arbitrary hosts/IPs.
SYNTHETIC_DNS_HOSTS = frozenset(
    {
        "arxiv.org",
        "www.arxiv.org",
        "export.arxiv.org",
        "doi.org",
        "www.doi.org",
        "dx.doi.org",
        "www.nature.com",
        "nature.com",
        "www.mdpi.com",
        "mdpi.com",
        "link.springer.com",
        "iopscience.iop.org",
        "academic.oup.com",
        "onlinelibrary.wiley.com",
        "link.aps.org",
        "bib-pubdb1.desy.de",
        "vtechworks.lib.vt.edu",
        "diposit.ub.edu",
        "journals.aps.org",
        "harvest.aps.org",
        "pubs.aip.org",
        "pubs.rsc.org",
        "pmc.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "europepmc.org",
        "www.osti.gov",
        "osti.gov",
        "hal.science",
        "hal.archives-ouvertes.fr",
        "hdl.handle.net",
        "zenodo.org",
        "doaj.org",
        "scipost.org",
        "www.scipost.org",
        "content.openalex.org",
    }
)


def retry_seconds(value, default=1):
    try:
        return max(default, float(value))
    except (ValueError, TypeError):
        try:
            return max(default, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return default


async def public_url(url):
    parsed = urlsplit(url)
    if parsed.hostname and parsed.hostname.startswith(("idp.", "login.", "auth.", "sso.")):
        raise AcquisitionError("AUTHENTICATION_ENDPOINT")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise AcquisitionError("UNSAFE_URL")
    if any(
        k.lower() in {"token", "api_key", "apikey", "password", "authorization"}
        for k, _ in parse_qsl(parsed.query)
    ):
        raise AcquisitionError("CREDENTIAL_BEARING_URL")
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            ),
            timeout=10,
        )
    except (OSError, TimeoutError):
        raise AcquisitionError("DNS_FAILURE", retryable=True) from None

    def allowed(address):
        ip = ipaddress.ip_address(address)
        return ip.is_global or (
            parsed.scheme == "https"
            and parsed.hostname in SYNTHETIC_DNS_HOSTS
            and ip in ipaddress.ip_network("198.18.0.0/15")
        )

    if not addresses or any(not allowed(a[4][0]) for a in addresses):
        raise AcquisitionError("NONPUBLIC_ADDRESS")


class PDFLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name", "").lower() == "citation_pdf_url" and a.get("content"):
            self.urls.append(a["content"])
        if tag == "link" and a.get("type") == "application/pdf" and a.get("href"):
            self.urls.append(a["href"])


class PublicDownloader:
    def __init__(self, root, options=None, client=None, validator=public_url, sleep=asyncio.sleep):
        self.root, self.options = root, options or AcquisitionOptions()
        self.client = client or self.new_client()
        self.validator, self.sleep = validator, sleep
        self.robots = {}
        self.trace = []
        self.state_dir = root / "acquisition" / "network"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.host_delays = {}
        self.arxiv_lock = None

    def new_client(self):
        return httpx.AsyncClient(
            timeout=self.options.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": AGENT, "Accept": "application/pdf,text/html;q=0.8,*/*;q=0.1"},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            auth=ContentAuth(credential("OPENALEX_API_KEY")),
        )

    async def close(self):
        await self.client.aclose()
        if self.arxiv_lock:
            self.arxiv_lock.__exit__()

    def state_path(self, host):
        return self.state_dir / (hashlib.sha256(host.encode()).hexdigest() + ".json")

    def load_state(self, host):
        path = self.state_path(host)
        return json.loads(path.read_text()) if path.exists() else {}

    def save_state(self, host, state):
        path = self.state_path(host)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state))
        temp.replace(path)

    async def pace(self, host):
        if host in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"}:
            if self.arxiv_lock is None:
                self.arxiv_lock = RunLock(Path("workspace/coordination/arxiv.lock"))
                try:
                    self.arxiv_lock.__enter__()
                except RuntimeError:
                    self.arxiv_lock = None
                    raise AcquisitionError("ARXIV_CONCURRENT_REQUEST", retryable=True) from None
            cooldowns = sorted(Path("workspace/coordination/arxiv-cooldowns").glob("*.json"), reverse=True)
            if cooldowns and json.loads(cooldowns[0].read_text())["until"] > time.time():
                raise AcquisitionError("ARXIV_SHARED_COOLDOWN", retryable=True)
        state = self.load_state(host)
        if state.get("blocked_until", 0) > time.time():
            raise AcquisitionError("HOST_COOLDOWN", retryable=True, details={"until": state["blocked_until"]})
        interval = max(
            self.options.interval, self.host_delays.get(host, 0), 3.2 if host.endswith("arxiv.org") else 0
        )
        await self.sleep(max(0, state.get("last_request", 0) + interval - time.time()))
        state["last_request"] = time.time()
        self.save_state(host, state)

    async def fetch(self, url, *, robots=False):
        """Bounded redirects and response bytes; all network attempts enter structured trace."""
        for redirect in range(6):
            if urlsplit(url).scheme == "http" and urlsplit(url).hostname in SYNTHETIC_DNS_HOSTS:
                url = "https:" + url[5:]
            await self.validator(url)
            parsed = urlsplit(url)
            host = parsed.hostname
            if not robots:
                await self.check_robots(url)
            next_url = None
            for attempt in range(1, self.options.attempts + 1):
                await self.pace(host)
                event = {
                    "url": url,
                    "attempt": attempt,
                    "time": datetime.now(UTC).isoformat(),
                    "robots": robots,
                }
                self.trace.append(event)
                try:
                    # Never reuse session cookies, including ones received from publishers.
                    self.client.cookies.clear()
                    async with self.client.stream(
                        "GET",
                        url,
                        timeout=httpx.Timeout(
                            self.options.timeout_seconds, pool=min(5, self.options.timeout_seconds)
                        ),
                    ) as response:
                        event.update(
                            status=response.status_code,
                            headers={
                                k: v
                                for k, v in response.headers.items()
                                if k in {"retry-after", "content-type", "content-length"}
                                or k.startswith(("x-ratelimit-", "x-rate-limit-"))
                            },
                        )
                        status = response.status_code
                        if status in {301, 302, 303, 307, 308}:
                            if not response.headers.get("location"):
                                raise AcquisitionError("REDIRECT_WITHOUT_LOCATION")
                            next_url = urljoin(url, response.headers["location"])
                            if parsed.scheme == "https" and urlsplit(next_url).scheme != "https":
                                raise AcquisitionError("HTTPS_DOWNGRADE")
                            event["redirect"] = next_url
                            break
                        if status in {401, 403, 429} or status >= 500:
                            delay = retry_seconds(response.headers.get("retry-after"), 2**attempt)
                            if status in {401, 403, 429} or response.headers.get("retry-after"):
                                state = self.load_state(host)
                                state["blocked_until"] = time.time() + max(
                                    delay, 300 if status == 429 else 60
                                )
                                self.save_state(host, state)
                                if status == 429 and host in {
                                    "arxiv.org",
                                    "export.arxiv.org",
                                    "www.arxiv.org",
                                }:
                                    write_new(
                                        Path("workspace/coordination/arxiv-cooldowns")
                                        / f"{time.time():.6f}-{uuid4().hex}.json",
                                        json.dumps({"until": state["blocked_until"], "http_status": 429}),
                                    )
                            if (
                                status >= 500
                                and not response.headers.get("retry-after")
                                and attempt < self.options.attempts
                            ):
                                await self.sleep(delay)
                                continue
                            raise AcquisitionError(f"HTTP_{status}", retryable=status == 429 or status >= 500)
                        if robots and status in {404, 410}:
                            return b"", url, status
                        if status != 200:
                            raise AcquisitionError(f"HTTP_{status}")
                        limit = 512 * 1024 if robots else self.options.max_bytes
                        size = 0
                        chunks = []
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise AcquisitionError("RESPONSE_TOO_LARGE")
                            chunks.append(chunk)
                        event["bytes"] = size
                        body = b"".join(chunks)
                        event["sha256"] = hashlib.sha256(body).hexdigest()
                        return body, url, status
                except (httpx.TimeoutException, httpx.NetworkError) as error:
                    event["error"] = type(error).__name__
                    if isinstance(error, httpx.PoolTimeout):
                        # A starved single-connection pool is a local transport failure,
                        # not evidence that every subsequent publisher is unavailable.
                        await self.client.aclose()
                        self.client = self.new_client()
                        event["pool_recreated"] = True
                    if attempt == self.options.attempts:
                        raise AcquisitionError("NETWORK_ERROR", retryable=True) from None
                    await self.sleep(2**attempt)
            if next_url:
                url = next_url
            else:
                raise AcquisitionError("RETRY_EXHAUSTED")
        raise AcquisitionError("REDIRECT_LIMIT")

    async def check_robots(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            body, _final, status = await self.fetch(origin + "/robots.txt", robots=True)
            parser = RobotFileParser()
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            if status in {404, 410}:
                parser.allow_all = True
            elif b"<html" in body[:1024].lower():
                raise AcquisitionError("ROBOTS_UNREADABLE")
            self.robots[origin] = parser
            self.host_delays[parts.hostname] = parser.crawl_delay(AGENT) or parser.crawl_delay("*") or 0
            archive = self.state_dir / (
                hashlib.sha256(origin.encode()).hexdigest()
                + "-"
                + hashlib.sha256(body).hexdigest()
                + "-robots.txt"
            )
            if not archive.exists():
                write_new(archive, body)
        if not self.robots[origin].can_fetch(AGENT, url):
            raise AcquisitionError("ROBOTS_DISALLOWED")

    async def download(self, candidate, target):
        self.trace = []
        if not candidate.eligible:
            raise AcquisitionError("OA_PERMISSION_UNCONFIRMED")
        if not target.resolve().is_relative_to((self.root / "downloads" / "program" / "staging").resolve()):
            raise AcquisitionError("NOT_A_PROGRAM_STAGING_PATH")
        try:
            async with asyncio.timeout(self.options.total_download_timeout):
                body, final, _ = await self.fetch(candidate.source_url)
                if not body.startswith(b"%PDF-") and not candidate.direct_pdf:
                    if len(body) > 2 * 1024 * 1024:
                        raise AcquisitionError("LANDING_PAGE_TOO_LARGE")
                    parser = PDFLinks()
                    parser.feed(body.decode("utf-8", errors="replace"))
                    links = [urljoin(final, u) for u in parser.urls]
                    links = [u for u in links if urlsplit(u).hostname == urlsplit(final).hostname]
                    if not links:
                        raise AcquisitionError("NO_DECLARED_SAME_HOST_PDF")
                    body, final, _ = await self.fetch(links[0])
                write_new(target, body)
                return {"final_url": final, "bytes": len(body), "trace": self.trace}
        except TimeoutError:
            raise AcquisitionError("TOTAL_DOWNLOAD_TIMEOUT", retryable=True) from None
