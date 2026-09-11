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
from pipeline.load import _host_of, is_test_booking, is_year_level
from pipeline.match import FLOOR
from pipeline.statuses import CARRIED_OVER, SEARCH_FOUND
from pipeline.triage import (ShareIndex, add_flag, classify_change,
                            gate_score, phase1_status)

# Flags marking rows for which no course page can exist.
UNSEARCHABLE_FLAGS = ("occupation_code_not_course", "year_level_not_course",
                      "test_booking_not_course")

# Statuses whose hold on a page may be taken by a clearly better match.
#
# Both rest on `gate_score` against a URL slug -- exactly what a challenger
# offers -- so comparing the two numbers is apples-to-apples. `verified` and
# `probable` are deliberately absent: those come from reading the
# Institution's own catalog, which is a different and stronger kind of
# evidence, and a slug score must not overturn it however wrong a given row
# looks.
EVICTABLE_STATUSES = (CARRIED_OVER, SEARCH_FOUND)

# How far a challenger must beat the current holder before taking its page.
# Measured on the 25 real displacements: the gaps cluster at 0.26-0.43, so
# this keeps near-ties out rather than admitting anything borderline.
EVICTION_MARGIN = 0.15

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


def cache_path_for(query: str, num: int = MAX_RESULTS,
                   cache_dir: str = SERPER_CACHE_DIR) -> str:
    """Where a response for *query* is cached.

    Module-level and public because the cache is deliberately un-indexed --
    the path is derived from the query and never listed -- so anything wanting
    to find an entry has to recompute it. `tools/find_cached_query.py` does,
    and importing this is what stops the tool and the provider disagreeing
    about where a file lives. A re-implementation would eventually drift and
    fail the worst way available: reporting "not cached" for a file that is
    right there.

    `num` is part of the key because asking for five results and ten are
    different questions. The `\x00` between them cannot occur in either field,
    so `num=5` with `"0foo"` cannot collide with `num=50` and `"foo"` the way
    plain concatenation would. `h[:2]` shards into 256 directories, because a
    flat one holding ~20,000 entries degrades on many filesystems.
    """
    h = hashlib.sha1(f"{num}\x00{query}".encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, h[:2], h + ".json.gz")


def read_cache_entry(path: str) -> dict | None:
    """The parsed cache file at *path*, or None if it is unusable.

    Anything unreadable -- absent, truncated, not JSON -- reads as a miss
    rather than raising. A damaged cache should cost a query, not a run.
    """
    try:
        if not os.path.exists(path):
            return None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            entry = json.load(fh)
        return entry if isinstance(entry, dict) else None
    except (OSError, ValueError, EOFError):
        return None


def links_from_entry(entry: dict) -> list[str] | None:
    """The result URLs an entry holds, in either cache format.

    Two formats exist and both stay readable:

    * `{"body": {...}}` -- the whole vendor response, written since the format
      changed. Links come out through `SerperProvider._links`, so the extraction
      rule has one definition whether a response arrives from the network or
      from disk.
    * `{"links": [...]}` -- links only, the original format. 510 entries were
      written this way and re-querying them would cost real money for data
      already paid for, so they are read as they are. They simply carry no
      title or snippet.

    Returns None when the entry has neither key, which reads as a miss.
    """
    if "body" in entry and isinstance(entry["body"], dict):
        return SerperProvider._links(entry["body"])
    if "links" in entry:
        return [u for u in entry["links"] if isinstance(u, str)]
    return None


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
        return cache_path_for(query, self.num, self.cache_dir)

    def _read_cache(self, query: str) -> list[str] | None:
        entry = read_cache_entry(self._cache_path(query))
        return None if entry is None else links_from_entry(entry)

    def _write_cache(self, query: str, body: dict) -> None:
        """Store the whole response, not just the links extracted from it.

        Keeping links only was cheaper by about 190 bytes an entry and cost
        two diagnoses: when a probe returned nothing for every row, the cache
        could not say whether the query or the parsing was at fault; and
        whether a result's title would rescue a weak slug could not be
        measured across 510 cached queries at all, because the titles were
        gone. `search()` still returns `list[str]`, so the provider interface
        is unchanged -- only what is kept on the way past.
        """
        path = self._cache_path(query)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump({"query": query, "body": body}, fh)
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
        print(f"aaaaaaa === {query}")
        cached = self._read_cache(query)
        print(f"cahced result === {cached}")
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
            print(f"serper body ====", body)
            print(f"serper status ====", status)
            print("json dump",json.dumps(body, indent=2)[:1500])

            if status in (401, 403):
                raise SearchProviderError(
                    f"Serper rejected the key (HTTP {status}) — check "
                    f"${SERPER_KEY_ENV}; no rows were filled")
            if status == 200:
                links = self._links(body)
                self._consecutive_errors = 0
                self._write_cache(query, body)
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

    False for rows already filled, for rows no course page can exist for, and
    for records the sheet has marked `(Inactive)`.

    The non-course rules are checked **twice over**: the flag, and the
    predicate itself. The flag alone was not enough. `_apply_flags` runs in
    `pipeline.load` when the *sheet* is read, but phase 2 reads an existing
    result file, so a rule added after that file was written never reaches it:
    `year_level_not_course` sat on 54 rows against the 584 `is_year_level`
    actually matches, and 530 school year bands were being paid for on every
    run. Re-deriving here makes a new rule bite immediately instead of waiting
    on a re-crawl.

    The flags are still written, and still checked first -- they are the
    durable record in the CSV of *why* a row was skipped, which a predicate
    evaluated at runtime cannot be.
    """
    if (row.get("course_url") or "").strip():
        return False
    flags = row.get("row_flags") or ""
    if any(f in flags for f in UNSEARCHABLE_FLAGS):
        return False
    if is_year_level(row.get("name", "")) or is_test_booking(row):
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
    # Pages taken from a weaker holder. Counted separately from `adopted`
    # because it moves a URL *between* courses rather than filling a blank,
    # and a number that does that must never be silent.
    evicted: int = 0
    # Set when a provider failure stopped the stage early. The rows adopted
    # before that point are kept: a broken vendor is no reason to discard
    # triage's work, but it must not be reported as a clean run either.
    aborted: str = ""


def _release_page(row: dict) -> None:
    """Undo a row's claim on a page it just lost, leaving it honest.

    The row must not be left looking like it never matched: its status goes
    back to what extraction actually concluded, the scores that justified the
    lost URL are cleared, and `url_lost_to_better_match` records that a
    stronger claim took the page rather than that nothing was found.
    """
    prior = (row.get("prior_course_url") or "").strip()
    row["course_url"] = ""
    row["matched_status"] = phase1_status(row)
    row["matched_score"] = ""
    row["match_margin"] = ""
    row["match_evidence"] = ""
    row["url_change"] = classify_change(prior, "")
    add_flag(row, "url_lost_to_better_match")


def _evictable(holders: list[tuple[str, str]], by_id: dict[str, dict],
               url: str, score: float, inst: str) -> list[dict] | None:
    """The holder rows a challenger scoring *score* may take *url* from.

    `None` means the page stays where it is -- which is the answer whenever a
    *single* holder is unbeatable, because taking a page from some holders and
    not others would leave the challenger sharing with exactly the rows the
    sharing rule refused it.
    """
    losers = []
    for rid, hname in holders:
        held = by_id.get(rid)
        if held is None:
            return None
        if (held.get("matched_status") or "").strip() not in EVICTABLE_STATUSES:
            return None
        if score < gate_score(hname, url, inst) + EVICTION_MARGIN:
            return None
        losers.append(held)
    return losers or None


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
       carried over cannot be handed to a second course as well -- unless
       every current holder is beatable, in which case the page changes hands
       (see `_evictable`). Placement is otherwise first-come-wins, and the
       rule as written refused a 1.0000 match in favour of a 0.5720 one that
       merely arrived earlier.

    `url_change` is recomputed on every acceptance. Triage writes that column
    before this stage runs, so a row filled here would otherwise keep a stale
    `none`/`dropped` while carrying a URL.

    **An eviction settles over two runs, not one.** Triage has already run by
    the time a page changes hands here, so anything that becomes legal because
    of the eviction is only picked up by the *next* run's triage: the loser
    re-offers its sheet URL and collects `adoption_denied_sharing`, and a
    Variant Sibling of the winner may adopt the freed page. Measured on the 11
    real evictions -- run 2 differs from run 1 in 12 rows, and runs 2, 3 and 4
    are byte-identical. This stage is a fixed point on its own; the two-run
    settle is the cost of deciding in file order in one pass rather than
    re-running triage inside it.
    """
    stats = SearchStats()
    by_id = {r["id"]: r for r in rows}

    for r in searchable_rows(rows, only_ids):
        site = _host_of(r.get("website", ""))
        print(f"serper search ====", site)
        if not site:
            continue
        name = r.get("name", "")
        inst = r.get("institution_name", "")

        stats.queried += 1
        try:
            results = provider.search(build_query(name, inst, site),
                                      site)[:MAX_RESULTS]
            print(f"sereper search result ====", results)
        except SearchProviderError as e:
            # The provider is broken, not merely empty-handed. Stop here and
            # let the caller report it; rows already filled stay filled.
            stats.aborted = str(e)
            break
        if not results:
            stats.no_results += 1
            continue

        on = [u for u in results if on_site(u, site)]
        print(f"on ======", on)
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
            # Placement is first-come-wins, and nothing re-contested a page
            # when a better claimant turned up later: `Bachelor of Commerce`
            # scored 1.0000 against its own page and stayed empty because
            # `Bachelor of Biomedicine` reached it first at 0.5720. The
            # sharing rule is right to refuse a second holder; what was
            # missing is asking whether the *first* one should still have it.
            losers = _evictable(index.holders_of(site, url), by_id, url,
                                best_score, inst)
            if losers is None:
                stats.rejected_by_sharing += 1
                add_flag(r, "search_denied_sharing")
                continue
            for held in losers:
                _release_page(held)
                index.release(site, url, held["id"])
            stats.evicted += len(losers)

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
