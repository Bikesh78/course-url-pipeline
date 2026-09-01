"""Polite, cached, resumable HTTP fetching.

Every response — including failures — is cached on disk, so an interrupted run
resumes for free and no URL is ever fetched twice. Politeness is not optional
here: CSU already returns 403 to a single plain request, so several of these
Sites react badly to load and a ban on a 456-row Institution is expensive.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field

USER_AGENT = ("Mozilla/5.0 (compatible; course-url-pipeline/1.0; "
              "+institution course catalogue mapping)")

DEFAULT_CACHE_DIR = ".cache"
DEFAULT_DELAY = 1.0          # seconds between requests to one domain
MAX_BYTES = 3_000_000        # listing pages run large; 3MB is a generous cap
TIMEOUT = 20


@dataclass
class FetchResult:
    """The outcome of one fetch, whether it succeeded or not.

    Failures are first-class and are cached like successes: a 404 is a fact
    about a URL worth remembering, and re-asking costs a request. The one
    exception is a transport error, where `status` is None — that is probably
    about us rather than the URL, so it is never cached.
    """

    url: str
    status: int | None
    final_url: str
    text: str
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        """200 *and* non-empty. An empty 200 is useless to the crawler."""
        return self.status == 200 and bool(self.text)


@dataclass
class FetchStats:
    """Counters for one run, reported at the end and in the coverage report.

    The ratio that matters is requests against cache hits: a re-run of already
    crawled Institutions should be almost entirely hits, and is the evidence
    that resuming is genuinely free.
    """

    requests: int = 0
    cache_hits: int = 0
    errors: int = 0
    blocked_by_robots: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def bump(self, name: str) -> None:
        """Increment a counter. Locked: worker threads share one instance."""
        with self.lock:
            setattr(self, name, getattr(self, name) + 1)


def domain_of(url: str) -> str:
    """The full host, subdomains included — "courses.aber.ac.uk".

    Not the rate-limiting key. Use `registrable()` for that, or a course
    subdomain gets its own request budget and the Institution is hit at twice
    the intended rate.
    """
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""


# Three-label public suffixes that appear in this dataset. Without these,
# `barkly.vic.edu.au` collapses to `vic.edu.au` and every Victorian school
# shares one bucket — harmless when the value is only a rate-limit key, but
# actively wrong now that it selects a Catalog. 429 hosts across 2,660 rows are
# affected.
#
# Deliberately an explicit list rather than a guess: `.taylors.edu.my` and
# `.bcu.ac.uk` are three-label *registrable domains* with subdomains, not
# suffixes, so a general "three labels ending in a short TLD" rule would be
# wrong in the other direction. This is not a full Public Suffix List and does
# not try to be.
_THREE_LABEL_SUFFIXES = frozenset({
    "vic.edu.au", "nsw.edu.au", "qld.edu.au", "wa.edu.au", "sa.edu.au",
    "tas.edu.au", "act.edu.au", "nt.edu.au", "catholic.edu.au",
    "nsw.gov.au", "vic.gov.au", "qld.gov.au", "wa.gov.au", "sa.gov.au",
    "tas.gov.au", "act.gov.au", "nt.gov.au",
    "on.ca", "qc.ca", "bc.ca", "ab.ca", "ns.ca", "mb.ca", "sk.ca",
})


def registrable(host: str) -> str:
    """Collapse subdomains so aber.ac.uk and courses.aber.ac.uk share a budget.

    Deliberately crude — it only needs to group hosts for rate limiting, not to
    be a correct public-suffix implementation.
    """
    host = host.lower().lstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # A known three-label suffix takes four labels, not three.
    if len(parts) >= 4 and ".".join(parts[-3:]) in _THREE_LABEL_SUFFIXES:
        return ".".join(parts[-4:])
    # Handle multi-part TLDs like .ac.uk, .edu.au, .co.nz
    if len(parts[-1]) == 2 and len(parts[-2]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class Fetcher:
    """Rate-limited, robots-aware, disk-cached fetcher."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR,
                 delay: float = DEFAULT_DELAY, respect_robots: bool = True,
                 offline: bool = False):
        self.cache_dir = cache_dir
        self.delay = delay
        self.respect_robots = respect_robots
        self.offline = offline
        self.stats = FetchStats()
        os.makedirs(cache_dir, exist_ok=True)
        self._domain_locks: dict[str, threading.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._registry_lock = threading.Lock()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # Institution sites routinely have imperfect certificate chains; a
        # verification failure must not cost us an entire Catalog.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ---------------------------------------------------------------- cache
    def _cache_paths(self, url: str) -> tuple[str, str]:
        """Return (gzipped path, legacy plain-JSON path) for *url*.

        Pages are stored gzipped: HTML compresses roughly eight-fold, and an
        uncompressed cache reached 454MB across only 12 Institutions, which
        extrapolates to several gigabytes for all 555. The plain path is still
        read so that a cache written before compression stays warm.
        """
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        stem = os.path.join(self.cache_dir, h[:2], h)
        return stem + ".json.gz", stem + ".json"

    @staticmethod
    def _result_from(d: dict, url: str) -> FetchResult:
        return FetchResult(url=d["url"], status=d["status"],
                           final_url=d["final_url"], text=d["text"],
                           from_cache=True, error=d.get("error", ""))

    def _read_cache(self, url: str) -> FetchResult | None:
        gz_path, plain_path = self._cache_paths(url)
        try:
            if os.path.exists(gz_path):
                with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                    return self._result_from(json.load(fh), url)
            if os.path.exists(plain_path):
                with open(plain_path, "r", encoding="utf-8") as fh:
                    return self._result_from(json.load(fh), url)
        except (OSError, ValueError, KeyError, EOFError):
            return None
        return None

    def _write_cache(self, res: FetchResult) -> None:
        gz_path, _ = self._cache_paths(res.url)
        os.makedirs(os.path.dirname(gz_path), exist_ok=True)
        tmp = gz_path + ".tmp"
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump({"url": res.url, "status": res.status,
                           "final_url": res.final_url, "text": res.text,
                           "error": res.error}, fh)
            os.replace(tmp, gz_path)
        except OSError:
            pass

    # -------------------------------------------------------------- politeness
    def _lock_for(self, key: str) -> threading.Lock:
        with self._registry_lock:
            if key not in self._domain_locks:
                self._domain_locks[key] = threading.Lock()
            return self._domain_locks[key]

    def _wait_turn(self, key: str) -> None:
        last = self._last_hit.get(key, 0.0)
        gap = time.monotonic() - last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_hit[key] = time.monotonic()

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = domain_of(url)
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{scheme}://{host}/robots.txt"
            try:
                raw = self._raw_get(robots_url, is_robots=True)
                if raw.status == 200 and raw.text.strip().lower().startswith(
                        ("user-agent", "#", "sitemap", "allow", "disallow", "crawl")):
                    rp.parse(raw.text.splitlines())
                else:
                    rp = None          # absent or an HTML 404 page: assume open
            except Exception:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def sitemaps_from_robots(self, url: str) -> list[str]:
        """Sitemap URLs an Institution's robots.txt declares, if any."""
        host = domain_of(url)
        scheme = urllib.parse.urlsplit(url).scheme or "https"
        raw = self.get(f"{scheme}://{host}/robots.txt")
        out = []
        if raw.ok:
            for line in raw.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    out.append(line.split(":", 1)[1].strip())
        return out

    # ----------------------------------------------------------------- fetch
    def _decode(self, raw: bytes, headers) -> str:
        enc = (headers.get("Content-Encoding") or "").lower()
        try:
            if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
        charset = None
        ctype = headers.get("Content-Type") or ""
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip()
        for cand in (charset, "utf-8", "latin-1"):
            if not cand:
                continue
            try:
                return raw.decode(cand)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "ignore")

    def _raw_get(self, url: str, is_robots: bool = False) -> FetchResult:
        key = registrable(domain_of(url))
        with self._lock_for(key):
            self._wait_turn(key)
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            })
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT,
                                            context=self._ssl_ctx) as resp:
                    raw = resp.read(MAX_BYTES)
                    text = self._decode(raw, resp.headers)
                    self.stats.bump("requests")
                    return FetchResult(url, resp.status, resp.geturl(), text)
            except urllib.error.HTTPError as e:
                self.stats.bump("requests")
                body = ""
                try:
                    body = self._decode(e.read(MAX_BYTES), e.headers)
                except Exception:
                    pass
                # 429/503 mean "slow down", so widen this domain's gap rather
                # than retrying into a ban.
                if e.code in (429, 503):
                    self._last_hit[key] = time.monotonic() + 30
                return FetchResult(url, e.code, url, body,
                                   error=f"HTTP {e.code}")
            except Exception as e:
                self.stats.bump("errors")
                return FetchResult(url, None, url, "", error=f"{type(e).__name__}: {e}")

    def get(self, url: str) -> FetchResult:
        """Fetch *url*, honouring cache, robots.txt and the per-domain delay."""
        if not url or not url.startswith(("http://", "https://")):
            return FetchResult(url, None, url, "", error="not an http url")

        cached = self._read_cache(url)
        if cached is not None:
            self.stats.bump("cache_hits")
            return cached

        if self.offline:
            return FetchResult(url, None, url, "", error="offline: cache miss")

        if not self._robots_allows(url):
            self.stats.bump("blocked_by_robots")
            res = FetchResult(url, None, url, "", error="blocked by robots.txt")
            self._write_cache(res)
            return res

        res = self._raw_get(url)
        # Never cache a transport error: it is probably about us, not the URL.
        if res.status is not None:
            self._write_cache(res)
        return res
