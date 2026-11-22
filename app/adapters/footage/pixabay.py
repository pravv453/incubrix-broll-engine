"""Pixabay Videos API adapter.

Policy notes (re-check the live terms before production; recorded per clip):
- Content is under the Pixabay Content License: commercial use allowed, attribution
  not required (we still record and offer it). Depicted people/brands may need separate
  clearance, so named-entity scenes never rely on a clip to imply endorsement.
- API rules: no hotlinking (we download to our own storage), cache search results
  for 24 h, ~100 requests/60 s. We cache, back off on 429 and honour Retry-After.
"""
import hashlib
import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.adapters.footage.base import ProviderError, apply_download_fault, apply_search_fault
from app.core.config import get_settings
from app.schemas.footage import Attribution, FootageCandidate, Licence

log = logging.getLogger("app.footage.pixabay")
API = "https://pixabay.com/api/videos/"
ALLOWED_MEDIA_HOSTS = {"cdn.pixabay.com", "player.vimeo.com", "i.vimeocdn.com", "pixabay.com"}
LICENCE = Licence(
    name="Pixabay Content License", url="https://pixabay.com/service/license-summary/",
    commercial_use_allowed=True, attribution_required=False, evidence_status="provider_policy_v1",
    restrictions=["Do not redistribute unaltered as stock", "Depicted people/trademarks may need separate clearance"],
)
MAX_DOWNLOAD = 250 * 1024 * 1024


class PixabayProvider:
    name = "pixabay"

    def __init__(self, cache_dir: Path) -> None:
        s = get_settings()
        if not s.pixabay_api_key:
            raise ProviderError(self.name, "PIXABAY_API_KEY not set", retryable=False)
        self.key = s.pixabay_api_key
        self.timeout = s.footage_request_timeout_s
        self.ttl = s.footage_cache_ttl_s
        self.cache = cache_dir / "pixabay"
        self.cache.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ http
    def _get(self, params: dict) -> dict:
        ck = self.cache / (hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest() + ".json")
        if ck.exists() and time.time() - ck.stat().st_mtime < self.ttl:
            return json.loads(ck.read_text())
        delay = 1.0
        for attempt in range(4):
            try:
                r = httpx.get(API, params={**params, "key": self.key}, timeout=self.timeout)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise ProviderError(self.name, f"network error: {type(exc).__name__}") from exc
            else:
                if r.status_code == 200:
                    data = r.json()
                    ck.write_text(json.dumps(data))
                    return data
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == 3:
                        raise ProviderError(self.name, f"HTTP {r.status_code}")
                    ra = r.headers.get("retry-after")
                    delay = float(ra) if ra and ra.isdigit() else delay
                else:
                    raise ProviderError(self.name, f"HTTP {r.status_code}", retryable=False)
            time.sleep(min(delay, 10) + random.random() * 0.3)
            delay *= 2
        raise ProviderError(self.name, "exhausted retries")

    def _map(self, hit: dict, query: str, rank: int) -> FootageCandidate | None:
        vids = hit.get("videos") or {}
        pick = None
        for size in ("medium", "large", "small"):  # medium is ~1080p; large may be 4K
            v = vids.get(size) or {}
            if v.get("url") and (v.get("width") or 0) >= 960:
                pick = (size, v)
                break
        if not pick:
            return None
        size, v = pick
        thumb = v.get("thumbnail") or (vids.get("small") or {}).get("thumbnail")
        user = hit.get("user") or "Pixabay contributor"
        page = hit.get("pageURL")
        return FootageCandidate(
            provider=self.name, asset_id=str(hit.get("id")), title=" ".join((hit.get("tags") or "").split(",")[:3]),
            tags=[t.strip() for t in (hit.get("tags") or "").split(",") if t.strip()],
            source_url=page, download_url=v["url"], thumbnail_url=thumb,
            width=v.get("width"), height=v.get("height"), duration_s=float(hit.get("duration") or 0) or None,
            licence=LICENCE.model_copy(),
            attribution=Attribution(required=False, creator=user, text=f"Video by {user} from Pixabay", url=page),
            watermarked=False, query=query, provider_rank=rank,
            extra={"rendition": size, "type": hit.get("type")},
        )

    # ------------------------------------------------------------------ api
    def search(self, query: str, per_page: int, orientation=None) -> list[FootageCandidate]:
        if apply_search_fault(self.name):
            return []
        data = self._get({"q": query[:100], "per_page": max(3, min(per_page, 50)), "safesearch": "true",
                          "order": "popular"})
        out = []
        for rank, hit in enumerate(data.get("hits", [])):
            c = self._map(hit, query, rank)
            if c:
                out.append(c)
        return out

    def get_asset(self, asset_id: str) -> FootageCandidate | None:
        data = self._get({"id": asset_id})
        hits = data.get("hits") or []
        return self._map(hits[0], "", 0) if hits else None

    def _fetch(self, url: str, dest: Path, limit: int) -> Path:
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_MEDIA_HOSTS:
            raise ProviderError(self.name, f"download host not allowlisted: {host}", retryable=False)
        size = 0
        try:
            with httpx.stream("GET", url, timeout=self.timeout * 3, follow_redirects=True) as r:
                if r.status_code != 200:
                    raise ProviderError(self.name, f"download HTTP {r.status_code}")
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        size += len(chunk)
                        if size > limit:
                            raise ProviderError(self.name, "download too large", retryable=False)
                        f.write(chunk)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"download failed: {type(exc).__name__}") from exc
        return dest

    def download(self, cand: FootageCandidate, dest: Path) -> Path:
        if apply_download_fault(self.name, dest):
            return dest
        return self._fetch(cand.download_url or "", dest, MAX_DOWNLOAD)

    def thumbnail(self, cand: FootageCandidate, dest: Path) -> Path | None:
        if not cand.thumbnail_url:
            return None
        try:
            return self._fetch(cand.thumbnail_url, dest, 5 * 1024 * 1024)
        except ProviderError:
            return None
