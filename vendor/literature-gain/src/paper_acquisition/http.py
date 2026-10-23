import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus
from uuid import uuid4

import httpx

from .config import credential
from .errors import (
    AuthenticationError,
    NetworkError,
    OfflineCacheMiss,
    QueryError,
    RateLimitError,
    RemoteError,
)
from .locking import RunLock
from .storage import write_new


class Transport:
    """One serial transport per provider; bounded retries and minimum spacing."""

    def __init__(self, settings, interval, observer, client=None, cache=None, provider=None):
        self.settings, self.interval, self.observer = settings, interval, observer
        self.cache, self.provider = cache, provider
        agent = "Paper-Acquisition/0.2 (physics and materials metadata research)"
        if contact := credential("PAPER_CONTACT_EMAIL"):
            agent += f" mailto:{contact}"
        self.client = client or httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": agent},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.last = 0.0
        self.last_raw_path = ""
        self.secrets: set[str] = set()
        self.requests = 0
        self.cache_hits = 0
        self.last_status = None
        self.access_status = None
        self.rate_headers = {}
        self.provider_lock = None

    async def close(self):
        await self.client.aclose()
        if self.provider_lock is not None:
            self.provider_lock.__exit__()

    async def request(self, method, url, *, params=None, headers=None, body=None, secret=False):
        cache_key = self.cache.key(method, url, params, headers, body) if self.cache and not secret else None
        cached = (
            self.cache.get(cache_key, allow_stale=self.settings.offline)
            if cache_key and not self.settings.force_refresh
            else None
        )
        if self.settings.offline and cached is None:
            raise OfflineCacheMiss("NO_ARCHIVED_RESPONSE_FOR_REQUEST")
        cooldown_root = self.settings.coordination_dir / "arxiv-cooldowns"
        if self.provider == "arxiv" and cached is None:
            if self.provider_lock is None:
                lock = RunLock(self.settings.coordination_dir / "arxiv.lock")
                try:
                    lock.__enter__()
                except RuntimeError:
                    self.access_status = "RATE_LIMITED"
                    raise RateLimitError("LOCAL_ARXIV_CONCURRENT_RUN") from None
                self.provider_lock = lock
            for path in sorted(cooldown_root.glob("*.json"), reverse=True)[:1]:
                if json.loads(path.read_text())["until"] > time.time():
                    self.access_status = "RATE_LIMITED"
                    raise RateLimitError("LOCAL_ARXIV_COOLDOWN")
        for key, value in (params or {}).items():
            if key.lower() in {"api_key", "apikey", "token", "key"} and value:
                self.secrets.add(str(value))
        for key, value in (headers or {}).items():
            if key.lower() in {"authorization", "x-api-key", "x-apikey", "x-els-apikey", "x-els-insttoken"}:
                self.secrets.add(str(value).split(" ", 1)[-1])
        if secret and isinstance(body, dict):
            self.secrets.update(str(v) for v in body.values() if v)
        for attempt in range(1, self.settings.attempts + 1):
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            delay = 2 ** (attempt - 1)
            try:
                kwargs = {"params": params, "headers": headers, "timeout": self.settings.timeout_seconds}
                if isinstance(body, dict):
                    kwargs["json"] = body
                elif body is not None:
                    kwargs["content"] = body
                if cached is not None:
                    response = cached
                    self.cache_hits += 1
                else:
                    response = await self.client.request(method, url, **kwargs)
                    self.requests += 1
                self.last_status = response.status_code
                self.rate_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k == "retry-after" or k.startswith(("x-ratelimit-", "x-rate-limit-"))
                }
                # Auth exchanges never persisted. Headers and secret query values never persisted.
                if not secret:
                    public_params = {
                        k: v
                        for k, v in (params or {}).items()
                        if k.lower() not in {"api_key", "apikey", "token", "key", "email"}
                    }
                    public_request = {"method": method, "url": url, "params": public_params, "body": body}
                    public_body = response.content
                    for value in sorted(self.secrets, key=len, reverse=True):
                        for encoded in {
                            value,
                            quote(value, safe=""),
                            quote_plus(value),
                            json.dumps(value)[1:-1],
                        }:
                            public_body = public_body.replace(encoded.encode(), b"[REDACTED]")
                    public_request["response_redacted"] = public_body != response.content
                    public_request["cache_hit"] = cached is not None
                    public_request["original_fetched_at"] = response.extensions.get("fetched_at")
                    archived = httpx.Response(
                        response.status_code,
                        content=public_body,
                        headers={
                            k: v
                            for k, v in response.headers.items()
                            if k.lower() not in {"content-encoding", "content-length"}
                        },
                    )
                    self.last_raw_path = self.observer(public_request, archived, attempt)
                status = response.status_code
                if status in (401, 403):
                    message = response.text.lower()
                    self.access_status = (
                        "PENDING_APPROVAL"
                        if "pending" in message or "approval" in message
                        else "NO_API_ENTITLEMENT"
                        if any(
                            t in message
                            for t in ("entitlement", "subscription", "not authorized", "insufficient")
                        )
                        else "PROVIDER_ERROR"
                    )
                    raise AuthenticationError(f"HTTP_{status}")
                if status == 429:
                    self.access_status = "RATE_LIMITED"
                    failure = RateLimitError("HTTP_429")
                elif status >= 500:
                    failure = RemoteError(f"HTTP_{status}")
                elif status >= 300:
                    raise QueryError(f"HTTP_{status}")
                else:
                    self.access_status = None
                    if cache_key and cached is None:
                        self.cache.put(cache_key, archived)
                    return response if secret else archived
                retry = response.headers.get("retry-after") or response.headers.get(
                    "x-rate-limit-retry-after-seconds"
                )
                if retry:
                    try:
                        delay = max(delay, float(retry))
                    except ValueError:
                        try:
                            delay = max(
                                delay, (parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds()
                            )
                        except (ValueError, TypeError):
                            pass
                # Do not retry earlier than a long server-requested wait; allow safe rerun later.
                if (
                    self.provider == "arxiv"
                    and status == 429
                    and (attempt == self.settings.attempts or delay > 60)
                ):
                    write_new(
                        cooldown_root / f"{time.time():.6f}-{uuid4().hex}.json",
                        json.dumps({"until": time.time() + max(300, delay), "http_status": 429}),
                    )
                if delay > 60:
                    raise failure
                if self.provider == "arxiv" and status == 429:
                    # Cool off; no tight loop, no host/IP rotation to evade the service limit.
                    delay = max(delay, 30 * (2 ** (attempt - 1)))
            except httpx.RequestError:
                failure = NetworkError("NETWORK_REQUEST_FAILED")
            if attempt == self.settings.attempts:
                raise failure
            logging.getLogger("paper_acquisition").warning(
                json.dumps(
                    {"event": "retry", "attempt": attempt, "error": type(failure).__name__, "delay": delay}
                )
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")
