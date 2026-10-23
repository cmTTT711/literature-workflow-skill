import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .storage import write_new


class ResponseCache:
    """Immutable successful metadata responses. Credentials never stored, only one-way request digest."""

    def __init__(self, root: Path, ttl: int):
        self.root, self.ttl = root, ttl

    def key(self, method, url, params, headers, body):
        return hashlib.sha256(
            json.dumps([method, url, params, headers, body], sort_keys=True).encode()
        ).hexdigest()

    def get(self, key, allow_stale=False):
        if not self.ttl and not allow_stale:
            return None
        for path in sorted((self.root / key).glob("*.json"), reverse=True):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if not allow_stale and time.time() - metadata["created"] > self.ttl:
                    return None
                body = path.with_suffix(".body").read_bytes()
                if hashlib.sha256(body).hexdigest() != metadata["sha256"]:
                    continue
                response = httpx.Response(metadata["status"], content=body, headers=metadata["headers"])
                response.extensions["cache_hit"] = True
                response.extensions["fetched_at"] = metadata["created"]
                return response
            except (OSError, ValueError, KeyError):
                continue
        return None

    def put(self, key, response):
        created = time.time()
        # Leave room for Windows paths inside a run directory; old cache names remain readable.
        path = self.root / key / f"{created:.6f}-{uuid4().hex[:16]}.json"
        headers = {
            k: v
            for k, v in response.headers.items()
            if k in {"content-type", "retry-after"} or k.startswith(("x-ratelimit-", "x-rate-limit-"))
        }
        write_new(path.with_suffix(".body"), response.content)
        write_new(
            path,
            json.dumps(
                {
                    "created": created,
                    "status": response.status_code,
                    "headers": headers,
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                }
            ),
        )
