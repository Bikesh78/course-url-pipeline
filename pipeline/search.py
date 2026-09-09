"""Search fallback for rows neither extraction nor the source sheet could fill.

Where this fits
---------------
After Phase 1 and prior-URL triage, **25,767 rows** still have no URL. Just
under three fifths of them (15,247) are `no_catalog`: the crawler could not read
the site at all, so no amount of crawl tuning reaches them. A search engine can,
because it has already crawled those sites.

The rule that keeps this safe
-----------------------------
**A search hit is a Candidate, not an answer.** It is scored against the course
name, checked against the Site's own domain, put through the Variant Sibling
sharing rule, and can never be reported `verified` on the strength of a search
ranking. "It was the top result" is not evidence about the course; it is
evidence about the search engine. This is the same discipline ADR-0001 applies
to crawled URLs, for the same reason: a plausible wrong URL is worse than a
blank, because nothing downstream can detect it.

What is implemented, and what is missing
---------------------------------------
`search_rows` is the stage: it scores, gates, sharing-checks and *writes* the
URL, so the discipline above is enforced in code rather than described. What is
missing is only a vendor. The provider is an interface: `NullProvider` is the
default and returns nothing, so the pipeline runs unchanged without a key and
this stage is a genuine no-op rather than a partial write; `FixtureProvider`
reads canned results so the whole adoption path is testable offline. A real
vendor implements one method and nothing else changes.

Rows deliberately not searched
------------------------------
The 5,008 rows flagged `occupation_code_not_course` are ANZSCO skilled-migration
occupation codes ("... - 411511 (subclass 186)"), not courses. No course page
exists to find, and querying for them would spend real money on a guaranteed
miss.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from pipeline.catalog import clean_url
from pipeline.fetch import registrable
from pipeline.load import _host_of
from pipeline.match import FLOOR
from pipeline.statuses import SEARCH_FOUND
from pipeline.triage import (ShareIndex, add_flag, classify_change,
                            gate_score)

# Flags marking rows for which no course page can exist.
UNSEARCHABLE_FLAGS = ("occupation_code_not_course", "year_level_not_course")

# The sheet marks a retired record by prefixing the *institution* name --
# "(Inactive) Fleming College Toronto". Never the course name: 0 course names
# carry it against 72 institution names. Only 28 of the 20,706 searchable rows
# are affected, so this is about not paying for a record the source itself has
# retired rather than about the money.
INACTIVE_MARKER = "(inactive)"

MAX_RESULTS = 5

# Re-exported from `pipeline.statuses`, which owns the vocabulary. A search
# result is never `verified`: verification means we fetched the page and
# confirmed it, which a ranking cannot stand in for.

SERPER_ENDPOINT = "https://google.serper.dev/search"
# The key is read from the environment and never from a CLI flag: `run.py`
# writes `vars(args)` into the `runs` table as JSON, so a flag would persist
# the credential in the database.
SERPER_KEY_ENV = "SERPER_API_KEY"
SERPER_CACHE_DIR = os.path.join(".cache", "serper")
SERPER_TIMEOUT = 20
SERPER_DELAY = 0.2       # seconds between paid calls
SERPER_RETRIES = 2       # additional attempts after a 429/5xx
# Serper's published rate at the volume this pipeline needs. Used only to put a
# number in the log; it is an estimate, not an invoice.
SERPER_COST_PER_QUERY = 0.001

# A run that keeps failing is burning money for nothing, so stop rather than
# work through 20,706 rows returning empty.
MAX_CONSECUTIVE_ERRORS = 10


class SearchProviderError(RuntimeError):
    """The provider is broken, as distinct from the query having no answer.

    Raised rather than swallowed because the two are worlds apart on a paid
    API: a wrong key would otherwise return nothing for every row and read as
    "search found nothing" in the log, which is the same failure ADR-0001
    guards against — a plausible-looking result that nothing downstream can
    detect as wrong.
    """


class SearchProvider(Protocol):
    """Anything that can turn a course name into candidate URLs on one site."""

    def search(self, query: str, site: str) -> list[str]:
        """Return candidate URLs, best first. Never raises for a miss."""
        ...


class NullProvider:
    """The default: no vendor configured, so no results.

    Present so that Phase 2 runs end to end without a key rather than failing,
    and so the absence of a vendor is visible in the counts instead of being an
    import error.
    """

    def search(self, query: str, site: str) -> list[str]:
        """Always empty."""
        return []


class FixtureProvider:
    """Canned results keyed by query, for tests and offline demonstration."""

    def __init__(self, fixtures: dict[str, list[str]] | None = None,
                 path: str | None = None):
        self.fixtures = dict(fixtures or {})
        if path:
            with open(path, encoding="utf-8") as fh:
                self.fixtures.update(json.load(fh))
        self.queries: list[str] = []

    def search(self, query: str, site: str) -> list[str]:
        """Return the canned list for *query*, recording that it was asked."""
        self.queries.append(query)
        return list(self.fixtures.get(query, []))


def _post_json(url: str, payload: dict, headers: dict,
               timeout: int = SERPER_TIMEOUT) -> tuple[int, dict]:
    """POST *payload* as JSON and return (status, decoded body).

    Stdlib only, so ADR-0003's no-pip-install guarantee still holds with a
    paid provider wired in. Separated from the provider so tests can inject a
    transport and exercise the whole path without a key or a network.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


class SerperProvider:
    """Google results via Serper, over stdlib HTTP, cached on disk.

    Three things this does beyond calling the API:

    **It caches every response**, successes and empty results alike, gzipped
    under `.cache/serper/` the way pages are cached. Phase 2 is re-run often —
    over 20,000 eligible rows at a tenth of a cent each, a re-run that re-asked
    would cost as much as the first. An empty result is a fact about a query
    worth remembering.

    **It refuses to fail quietly.** A bad key raises rather than returning
    nothing, because 20,706 rows of "no results" is indistinguishable in the
    log from a search engine that genuinely found nothing. Repeated transport
    failures stop the run for the same reason.

    **It can be capped.** `limit` bounds paid calls per run, so a first live
    run can be tried on a few hundred rows before committing to the full sheet.
    """

    def __init__(self, api_key: str | None = None,
                 cache_dir: str = SERPER_CACHE_DIR,
                 delay: float = SERPER_DELAY, limit: int | None = None,
                 num: int = MAX_RESULTS,
                 transport: Callable[..., tuple[int, dict]] = _post_json):
        self.api_key = api_key if api_key is not None else os.environ.get(
            SERPER_KEY_ENV, "")
        if not self.api_key:
            raise SearchProviderError(
                f"no Serper key: put {SERPER_KEY_ENV}=... in .env, or set it "
                f"in the environment. Not a CLI flag — run args are persisted "
                f"to the database")
        self.cache_dir = cache_dir
        self.delay = delay
        self.limit = limit
        self.num = num
        self.transport = transport
        self.paid_calls = 0
        self.cache_hits = 0
        self.errors = 0
        self.capped = 0
        self._consecutive_errors = 0
        self._last_call = 0.0

    # ------------------------------------------------------------------ cache
    def _cache_path(self, query: str) -> str:
        h = hashlib.sha1(f"{self.num}\x00{query}".encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, h[:2], h + ".json.gz")

    def _read_cache(self, query: str) -> list[str] | None:
        path = self._cache_path(query)
        try:
            if os.path.exists(path):
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return list(json.load(fh)["links"])
        except (OSError, ValueError, KeyError, EOFError):
            return None
        return None

    def _write_cache(self, query: str, links: list[str]) -> None:
        path = self._cache_path(query)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump({"query": query, "links": links}, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    # ----------------------------------------------------------------- calling
    def _wait_turn(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    @staticmethod
    def _links(body: dict) -> list[str]:
        """The organic result URLs, best first, ignoring ads and snippets."""
        return [hit["link"] for hit in (body.get("organic") or [])
                if isinstance(hit, dict) and hit.get("link")]

    def search(self, query: str, site: str) -> list[str]:
        """Return candidate URLs for *query*, best first. Empty on a miss."""
        cached = self._read_cache(query)
        if cached is not None:
            self.cache_hits += 1
            return cached

        if self.limit is not None and self.paid_calls >= self.limit:
            self.capped += 1
            return []

        payload = {"q": query, "num": self.num}
        headers = {"X-API-KEY": self.api_key}

        for attempt in range(SERPER_RETRIES + 1):
            self._wait_turn()
            self.paid_calls += 1
            status, body = self.transport(SERPER_ENDPOINT, payload, headers)

            if status in (401, 403):
                raise SearchProviderError(
                    f"Serper rejected the key (HTTP {status}) — check "
                    f"${SERPER_KEY_ENV}; no rows were filled")
            if status == 200:
                links = self._links(body)
                self._consecutive_errors = 0
                self._write_cache(query, links)
                return links
            # 429 and 5xx are "slow down" or "try later", so back off and
            # retry; anything else is not worth a second paid call.
            if status not in (429, 500, 502, 503, 504):
                break
            if attempt < SERPER_RETRIES:
                time.sleep(self.delay * (2 ** (attempt + 1)))

        self.errors += 1
        self._consecutive_errors += 1
        if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            raise SearchProviderError(
                f"{self._consecutive_errors} consecutive Serper failures "
                f"(last HTTP {status}) — stopping rather than spending "
                f"further; {self.paid_calls} calls made")
        return []

    def report(self) -> str:
        """One line on what this cost, for the run log."""
        est = self.paid_calls * SERPER_COST_PER_QUERY
        bits = [f"{self.paid_calls} paid calls (~${est:.2f})",
                f"{self.cache_hits} from cache"]
        if self.errors:
            bits.append(f"{self.errors} failed")
        if self.capped:
            bits.append(f"{self.capped} skipped by --search-limit")
        return ", ".join(bits)


def build_query(name: str, institution: str, site: str) -> str:
    """The query string for one course: the name, scoped to the Site.

    Neither quoted nor institution-qualified, and both omissions are measured
    rather than assumed. Against 40 rows of the first trial batch, asking for
    the exact phrase *and* naming the institution returned a result that
    cleared the 0.55 gate for **5 of 40** rows; the bare name scoped with
    `site:` cleared it for **10 of 40**. Twice the usable yield.

    Why each hurt:

    * **The phrase quotes** demand the site word the course exactly as the
      sheet does. "Certificate IV in Engineering" quoted against a small RTO
      returns nothing at all, though the course is there under another
      spelling.
    * **The institution name** is redundant once `site:` has scoped the query,
      and it is the *legal* name — "A2 Education Pty Ltd" — which rarely
      appears in a course page's own text, so it filters out real pages.

    Recall is the engine's job and precision is ours: every on-site result in
    that measurement passed the domain check, so `gate_score` and the floor did
    all the filtering. Letting the engine be generous and staying strict
    afterwards is the same division of labour the module docstring describes —
    a hit is a Candidate, not an answer.
    """
    name = re.sub(r"\s+", " ", (name or "").strip())
    parts = [name]
    if site:
        parts.append(f"site:{site}")
    return " ".join(p for p in parts if p)


def is_searchable(row: dict) -> bool:
    """Is this row worth spending a paid query on?

    False for rows already filled, for rows whose flags say no course page can
    exist, and for records the sheet has marked `(Inactive)`.
    """
    if (row.get("course_url") or "").strip():
        return False
    flags = row.get("row_flags") or ""
    if any(f in flags for f in UNSEARCHABLE_FLAGS):
        return False
    # Checked on the course name too, defensively: the marker is on the
    # institution in this sheet, but it costs nothing to honour either.
    return not any(INACTIVE_MARKER in (row.get(f) or "").lower()
                   for f in ("institution_name", "name"))


def searchable_rows(rows: list[dict],
                    only_ids: set[str] | None = None) -> list[dict]:
    """The subset of *rows* a search vendor would be asked about.

    `only_ids` restricts the result to one batch of course ids, so a trial can
    spend a fixed budget on a chosen sample -- see
    `tools/sample_search_targets.py`. It narrows, never widens: a row in the
    batch that is not searchable on its own terms is still skipped.
    """
    picked = [r for r in rows if is_searchable(r)]
    if only_ids is None:
        return picked
    return [r for r in picked if r["id"] in only_ids]


def on_site(url: str, site: str) -> bool:
    """Is this result on the Institution's own registrable domain?

    A search engine will happily return an aggregator's page for the course.
    That is never a valid result here, for the same reason a crawled off-site
    URL is dropped.
    """
    if not url or not site:
        return False
    try:
        host = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return False
    return bool(host) and registrable(host) == registrable(site)


@dataclass
class SearchStats:
    """What the search stage did, for the run log and for verification."""

    queried: int = 0
    no_results: int = 0
    adopted: int = 0
    rejected_off_site: int = 0
    rejected_by_gate: int = 0
    rejected_by_sharing: int = 0
    # Set when a provider failure stopped the stage early. The rows adopted
    # before that point are kept: a broken vendor is no reason to discard
    # triage's work, but it must not be reported as a clean run either.
    aborted: str = ""


def search_rows(rows: list[dict], provider: SearchProvider,
                index: ShareIndex, floor: float = FLOOR,
                only_ids: set[str] | None = None) -> SearchStats:
    """Fill still-empty rows from search results, in place, as Candidates.

    Every hit clears four independent checks before it is written, and the
    order is deliberate — cheapest first, and nothing is scored before it is
    known to be on the Institution's own domain:

    1. `on_site` — an aggregator's page for the course is never the answer.
    2. **The best-scoring hit wins, not the first.** Ranking is evidence about
       the search engine, not about the course, so every surviving hit is
       scored against the course name and the highest takes it; the provider's
       order is a tiebreak only.
    3. The existing 0.55 floor, the same one extraction and triage answer to.
    4. The sharing rule, against the *shared* index, so a page triage already
       carried over cannot be handed to a second course as well.

    `url_change` is recomputed on every acceptance. Triage writes that column
    before this stage runs, so a row filled here would otherwise keep a stale
    `none`/`dropped` while carrying a URL.
    """
    stats = SearchStats()

    for r in searchable_rows(rows, only_ids):
        site = _host_of(r.get("website", ""))
        if not site:
            continue
        name = r.get("name", "")
        inst = r.get("institution_name", "")

        stats.queried += 1
        try:
            results = provider.search(build_query(name, inst, site),
                                      site)[:MAX_RESULTS]
        except SearchProviderError as e:
            # The provider is broken, not merely empty-handed. Stop here and
            # let the caller report it; rows already filled stay filled.
            stats.aborted = str(e)
            break
        if not results:
            stats.no_results += 1
            continue

        on = [u for u in results if on_site(u, site)]
        stats.rejected_off_site += len(results) - len(on)
        if not on:
            continue

        best, best_score = "", -1.0
        for url in on:
            g = gate_score(name, url, inst)
            if g > best_score:
                best, best_score = url, g

        if best_score < floor:
            stats.rejected_by_gate += 1
            continue

        url = clean_url(best)
        if index.would_break(site, url, name):
            stats.rejected_by_sharing += 1
            add_flag(r, "search_denied_sharing")
            continue

        r["course_url"] = url
        r["matched_status"] = SEARCH_FOUND
        r["matched_score"] = f"{best_score:.4f}"
        # No runner-up semantics here: Margin compares two Candidates ranked by
        # the same evidence, and a search ranking is not that.
        r["match_margin"] = ""
        r["match_evidence"] = (f"search result on {site} "
                               f"(provider: {type(provider).__name__})")
        add_flag(r, "url_from_search")
        r["url_change"] = classify_change(
            (r.get("prior_course_url") or "").strip(), url)
        index.move(site, "", url, r["id"])
        stats.adopted += 1

    return stats
