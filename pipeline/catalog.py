"""Catalog discovery and extraction.

Builds an Institution's Catalog by crawling its own site. A Catalog is always
*extracted*, never generated from a URL pattern — see ADR-0001, which records
the measurement that disqualified generation.

Domain terms (Catalog, Candidate, Extraction Health) are defined in
`CONTEXT.md`. The words below are specific to *this* crawler and are defined
here rather than there, because they would become wrong if the extraction
strategy were replaced, whereas the domain terms would not.

Terminology
-----------
**hub path**
    A *guess*: one of `HUB_PATHS` ("/courses", "/study", ...) probed against
    the Institution's site. Most guesses are wrong — a typical Institution
    answers 200 to three or four of the sixteen.

**seed**
    A hub path or course subdomain that answered 200 and survived filtering.
    Seeds are where crawling starts, and `Catalog.seed_yield` attributes
    Candidates back to the seed they were reached from, so an unproductive
    probe can be spotted without guessing why it was useless.

**listing page**
    Any page the crawl fetches and harvests links from. It is **not** a page
    type the crawler recognises: every fetched page is treated identically.
    The name describes intent, not a test — and the intent is often wrong.
    Measured on Aberystwyth:

        courses.aber.ac.uk/                        98 links,  12 course-links
        courses.aber.ac.uk/undergraduate/data-science
                                                  151 links,  48 course-links

    The "course page" is four times richer than the "listing", because
    universities put related-course and module lists on course pages. Harvest
    from everything; let scoring discard the junk.

**course page**
    The destination a Candidate points at — what ends up in `course_url`.
    A course page is also a listing page in the sense above, which is exactly
    why no page-type test exists.

**ladder**
    The ordered fallback of extraction strategies below. Each rung is weaker
    evidence about what a course is *called* than the one before, so the ladder
    is climbed down only when forced, and stops the moment Extraction Health
    passes:

      1. `crawl_listings` — bounded BFS from the seeds. Names come from the
         site's own anchor text. The good rung; all 12 current Catalogs use it.
      2. `harvest_sitemaps` — names reverse-engineered from URL slugs, so these
         Candidates carry `source="sitemap"` and can never reach `verified`.
      3. `harvest_json_endpoints` — the API behind a JavaScript course finder,
         for sites that publish nothing in their HTML.

An Institution whose Catalog fails Extraction Health is reported `no_catalog`
with a `failure_reason`, rather than being matched against a partial Catalog.
"""

from __future__ import annotations

import heapq
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from pipeline.fetch import Fetcher, registrable
from pipeline.normalize import level_of, normalize_name

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except ImportError:                                    # pragma: no cover
    _HAVE_BS4 = False

# Paths that commonly root a course listing. Ordered by how often they were the
# productive one across the top Institutions during planning.
HUB_PATHS = [
    "/courses", "/study", "/courses/", "/study/", "/course-search",
    "/undergraduate", "/postgraduate", "/study/courses", "/programs",
    "/programmes", "/study-with-us", "/our-courses", "/course-finder",
    "/study/undergraduate", "/study/postgraduate", "/academics",
]

# Course subdomains are common and are not reachable by probing paths on www.
HUB_SUBDOMAINS = ["courses", "study", "programs", "programmes", "catalogue",
                  "catalog", "handbook"]

# Matched as a *substring* of the path, not as a whole segment. Institutions
# embed these words inside compound segments — Coventry uses
# "/study-at-coventry/undergraduate-study/" and "/course-structure/..." — and a
# whole-segment rule rejected all 188 plausible course links on its hub page,
# leaving the Catalog to be filled from an unrelated sub-site's sitemap. Being
# permissive here is safe: junk Candidates are eliminated by Score, Margin and
# URL specificity downstream, whereas a Candidate never collected is
# unrecoverable.
_COURSE_PATH = re.compile(
    r"(course|program|programme|degree|study|undergrad|postgrad|subject"
    r"|qualification|catalogue|catalog|handbook|bachelor|master|diploma"
    r"|certificate)", re.IGNORECASE)

# Paths that look like a course *listing* rather than a single course page.
# Crawling these first spends the page budget where Candidates actually are.
_LISTING_HINT = re.compile(
    r"(course-structure|course-search|course-finder|courses?/|/a-z|programme-"
    r"|program-|undergraduate|postgraduate|subject|study/|/study$|catalogue"
    r"|catalog|handbook|browse)", re.IGNORECASE)

# Anchor text that marks a complete course index. These pages are the single
# most productive thing to crawl, and their URLs often look nothing like a
# course path ("/clearing/course-finder/"), so they are found by their label.
_INDEX_ANCHOR = re.compile(
    r"\b(a-?z|a to z|course finder|course search|find a course|all courses"
    r"|browse courses|course list|search for a course|explore courses"
    r"|our courses|course catalogue|course catalog|program finder"
    r"|programme finder|view all courses|full list)\b", re.IGNORECASE)

# Anchor text that is navigation, never a course name.
NAV_STOPLIST = {
    "study", "courses", "course", "undergraduate", "postgraduate",
    "postgraduate taught", "postgraduate research", "preparation courses",
    "request prospectus", "request a prospectus", "apply", "apply now",
    "home", "contact", "contact us", "search", "more", "read more",
    "find out more", "view all", "view all courses", "see all", "next",
    "previous", "back", "overview", "about", "about us", "news", "events",
    "students", "staff", "alumni", "library", "research", "international",
    "fees", "fees and funding", "scholarships", "accommodation", "open day",
    "open days", "how to apply", "entry requirements", "student life",
    "why choose us", "our campus", "campus", "login", "log in", "register",
    "privacy", "cookies", "terms", "sitemap", "accessibility", "skip to content",
    "menu", "toggle menu", "close", "all courses", "browse courses",
    "subjects", "faculties", "schools", "departments", "a-z", "a to z",
}

# Substring patterns for anchor text that is page furniture rather than a
# course. Accessibility skip-links pass a word-count test, so they need
# explicit exclusion.
NAV_SUBSTRINGS = (
    "skip navigation", "skip to", "go straight to", "main content",
    "cookie", "javascript", "screen reader", "toggle", "search this site",
    "back to top", "download the", "request information", "book an open",
    "enquire now", "chat with", "virtual tour", "order a prospectus",
)

_LEVEL_FROM_PATH = [
    (re.compile(r"/(?:undergraduate|ug|bachelor)s?(?:/|$)", re.I), "ug"),
    (re.compile(r"/(?:postgraduate|pg|master|masters|graduate|research|phd|"
                r"doctoral)(?:/|$)", re.I), "pg"),
]

# Bumped whenever the Catalog JSON gains a field the pipeline relies on, so
# that stale cached Catalogs are rebuilt rather than silently disabling a check.
# v2 added `domains`, which the domain-containment rail needs.
# v3 added `failure_reason` and `seed_yield`, which the coverage report needs to
# tell a blocked site apart from a mistargeted crawl.
SCHEMA_VERSION = 3

# The page budget is genuinely binding for large Institutions: Coventry has 465
# Course Rows, and a listing page yields perhaps 20-40 Candidates, so the crawl
# must reach many listing pages. 80 was too few and made the result depend on
# which pages happened to be discovered first.
# Seeds that are sign-in walls rather than listings. Observed as
# `catalogue.abertay.ac.uk/mng/login`, which answered 200 and yielded nothing.
_AUTH_PATH = re.compile(r"/(?:login|signin|sign-in|auth|account|logon)(?:/|$)",
                        re.IGNORECASE)

# HTTP statuses that mean "we are being refused", as opposed to "wrong guess".
_REFUSAL_STATUSES = frozenset({401, 403, 429, 451})
_REFUSAL_SHARE = 0.8


def classify_probes(status_counts: dict[int, int]) -> str:
    """Read hub-probe statuses as an access verdict. Pure; no network.

    Distinguishes a site that refuses the crawler from one where we simply
    guessed the wrong paths. ACU answers 403 to all 16 hub paths, while every
    other Institution measured returns at least three 200s — so the categories
    are far apart on real data. The threshold is a share rather than "all", to
    tolerate a site that leaks the odd 404 among its refusals.

    Returns "blocked", "no_hub", or "" (at least one hub responded).
    """
    resolved = sum(status_counts.values())
    if not resolved:
        return ""
    refusals = sum(n for code, n in status_counts.items()
                   if code in _REFUSAL_STATUSES)
    if refusals >= _REFUSAL_SHARE * resolved:
        return "blocked"
    if not status_counts.get(200):
        return "no_hub"
    return ""


MAX_PAGES_PER_INSTITUTION = 160
MAX_DEPTH = 2
MIN_CANDIDATES = 5
HEALTH_RATIO = 0.40


@dataclass
class Candidate:
    """One course the Institution publishes: a `(name, url)` pair we found.

    The supply side of the match — Course Rows are the demand side. See
    `CONTEXT.md` for the domain definition.

    A Candidate's **identity is its URL**, not its name. That is what the
    uniqueness rail of ADR-0002 claims: two Course Rows may not both be
    assigned the same URL. Two Candidates sharing a URL are the same Candidate.

    Attributes:
        name: anchor text the site printed next to the link, so it carries
            listing furniture like "(BSc, 3 years)". Normalisation strips that.
        url: the proposed answer, and the identity.
        level: "ug" or "pg", read from the URL path or inferred from the Award.
            Used to stop an undergraduate row taking a postgraduate page.
        source: where the *name* came from, which is an evidence-quality
            marker: "listing" (anchor text, trustworthy), "sitemap" (guessed
            from the URL slug, so demoted and never `verified`), or "json"
            (a field in a course-finder API).

    Candidates are deliberately over-collected. A Catalog contains navigation
    ("Fees and Finance") and module names alongside real courses, because junk
    is cheap to score away while a Candidate never collected is unrecoverable.
    """

    name: str
    url: str
    level: str | None = None
    source: str = "listing"

    @property
    def key(self) -> str:
        """The Candidate's identity: its URL. See the class docstring."""
        return self.url


@dataclass
class Catalog:
    """Everything one Institution publishes, as far as extraction could reach.

    The **closed universe** a Course Row is resolved against: matching selects
    from `candidates` and nothing else, which is what makes `no_match` a
    meaningful statement ("absent from this set") rather than a shrug.

    One Catalog per Institution, cached as `catalogs/<slug>.json`. Extraction
    is the expensive stage, so persisting it lets the entire matching half of
    the pipeline be re-run offline in seconds.

    A Catalog is *not* a promise of completeness. Ten of the twelve currently
    cached hit the `MAX_PAGES_PER_INSTITUTION` budget exactly, meaning their
    crawls stopped when they ran out of pages to spend rather than out of
    courses to find.

    Attributes:
        candidates: the answer space. See `Candidate`.
        seeds: listing roots the crawl started from.
        strategy: which ladder rungs contributed, e.g. "listing+sitemap".
        pages_fetched: crawl cost; equal to the page budget means truncated.
        notes: human-readable record of what happened, including failures.
        domains: hosts this Institution may yield URLs on. Recorded at
            extraction time because seeds legitimately redirect off the www
            host (ANU -> study.anu.edu.au) and that is unknowable later.
        failure_reason: why extraction fell short, if it did. Describes
            *extraction only* — "mistargeted", which depends on the eventual
            fill rate, is computed in report.py instead.
        seed_yield: Candidates attributed to the seed they were reached from,
            counting the whole BFS subtree rather than the single page. A seed
            with zero yield is how a useless probe reveals itself.
    """

    institution: str
    candidates: list[Candidate] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    strategy: str = ""
    pages_fetched: int = 0
    notes: list[str] = field(default_factory=list)
    # Registrable domains this Institution is allowed to yield URLs on. Seeds
    # legitimately redirect off the www host (Adelaide -> adelaide.edu.au,
    # ANU -> study.anu.edu.au), so the permitted set is recorded at extraction
    # time rather than inferred later.
    domains: list[str] = field(default_factory=list)
    # Why extraction fell short, when it did. Describes *extraction only* —
    # "mistargeted" depends on the eventual fill rate and so is computed in
    # report.py, not here.
    failure_reason: str = ""
    # Candidates attributed to the seed they were reached from. A seed with
    # zero yield is the evidence that exposes a useless probe (a library
    # catalogue, a login shell) without having to guess what it is.
    seed_yield: dict[str, int] = field(default_factory=dict)

    def healthy(self, expected_rows: int) -> bool:
        """Is this Catalog plausibly complete enough to match against?

        Judged by Candidate count against the Institution's Course Row count.
        Aberystwyth extracted 398 against 456 rows (healthy); Abertay extracted
        13 against 224 (a failed extraction, escalated rather than used).
        """
        if len(self.candidates) < MIN_CANDIDATES:
            return False
        return len(self.candidates) >= HEALTH_RATIO * expected_rows

    def as_dict(self) -> dict:
        """Serialise for `catalogs/<slug>.json`, stamped with SCHEMA_VERSION."""
        return {
            "schema_version": SCHEMA_VERSION,
            "institution": self.institution,
            "strategy": self.strategy,
            "seeds": self.seeds,
            "pages_fetched": self.pages_fetched,
            "notes": self.notes,
            "domains": self.domains,
            "failure_reason": self.failure_reason,
            "seed_yield": self.seed_yield,
            "candidates": [{"name": c.name, "url": c.url, "level": c.level,
                            "source": c.source} for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Catalog":
        """Rebuild from cached JSON, defaulting fields older versions lack.

        Tolerating absent fields matters because `run.py` only rebuilds a
        Catalog when SCHEMA_VERSION rises; anything it chooses to reuse must
        load without exploding.
        """
        cat = cls(institution=d.get("institution", ""),
                  seeds=d.get("seeds", []),
                  strategy=d.get("strategy", ""),
                  pages_fetched=d.get("pages_fetched", 0),
                  notes=d.get("notes", []),
                  domains=d.get("domains", []),
                  failure_reason=d.get("failure_reason", ""),
                  seed_yield=d.get("seed_yield", {}))
        cat.candidates = [Candidate(c["name"], c["url"], c.get("level"),
                                    c.get("source", "listing"))
                          for c in d.get("candidates", [])]
        return cat


# --------------------------------------------------------------------------
# HTML link extraction
# --------------------------------------------------------------------------
_A_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                   re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", fragment)).strip()


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """All (anchor text, absolute URL) pairs on a page."""
    out: list[tuple[str, str]] = []
    if _HAVE_BS4:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            out.append((re.sub(r"\s+", " ", a.get_text(" ")).strip(),
                        urllib.parse.urljoin(base_url, a["href"])))
    else:                                              # pragma: no cover
        for href, inner in _A_RE.findall(html):
            out.append((_text(inner), urllib.parse.urljoin(base_url, href)))
    return out


def page_title(html: str) -> str:
    """The page's `<title>`, flattened. Empty string when absent.

    Used as Verification evidence only when `<h1>` is missing, since a title
    usually carries the Institution name and course code as noise.
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return _text(m.group(1)) if m else ""


def page_heading(html: str) -> str:
    """The page's first `<h1>`, flattened. Empty string when absent.

    Preferred over `<title>` for Verification: an `<h1>` is typically the bare
    course name ("Data Science") where the title is
    "Aberystwyth University - Data Science 7G73 BSc".
    """
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    return _text(m.group(1)) if m else ""


# Query parameters that select a *view* of one course rather than identifying a
# different course. Coventry links the same page as "?term=2026-27" and
# "?term=2027-28"; keeping them makes one course look like several rival
# Candidates and drives the Margin to zero.
_VIEW_PARAMS = {"term", "year", "academicyear", "intake", "start", "startdate",
                "campus", "mode", "studymode", "attendance", "from", "ref",
                "source", "referrer", "lang"}


def clean_url(url: str) -> str:
    """Drop fragments and tracking noise so the same page has one identity."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
         if k.lower() not in _VIEW_PARAMS
         and not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, ""))


def level_from_url(url: str) -> str | None:
    """Read "ug"/"pg" from the URL path, or None if it says nothing.

    More reliable than inferring Level from the Award, because it is the
    Institution's own filing decision. It disagrees with the Award for
    integrated masters — Aberystwyth files its four-year MAg under
    /undergraduate/ — and the URL is right in that dispute.
    """
    for pattern, lvl in _LEVEL_FROM_PATH:
        if pattern.search(url):
            return lvl
    return None


# Path segments that mark a hub or category page rather than a course page.
# A subject hub ("/study-with-us/subjects/agriculture/") carries exactly the
# subject's name and so scores identically to the real course page, which
# collapses the Margin. Hubs are demoted rather than excluded, because some
# Institutions genuinely list courses under these paths.
# Substring-matched for the same reason as _COURSE_PATH: Coventry's subject
# hubs live at "/undergraduate-subjects/<subject>/", which a whole-segment rule
# misses, letting a subject page be reported `verified` for a specific degree.
_HUB_PATH = re.compile(
    r"(subject|study-with-us|facultie|faculty|areas-of-study|a-z-of|/a-z"
    r"|browse|explore-our|schools-and-departments|colleges-and-schools)",
    re.IGNORECASE)

_LEVEL_PATH = re.compile(
    r"/(?:undergraduate|postgraduate|course|courses|program|programs"
    r"|programme|programmes|degree|degrees)/[^/]+", re.IGNORECASE)


# How much a Candidate's Score is discounted for the *shape* of its URL. These
# are Scoring weights and belong with the others in `pipeline/normalize.py`
# conceptually, but they live here because only this module knows what a URL
# path means. Applied in `pipeline/match.py`, not inside `normalize.score()`.
#
# A subject hub carries exactly the subject's name, so it ties with the real
# course page on text alone and wins on nothing; demotion is what breaks that
# tie. Demotion rather than exclusion, because some Institutions genuinely
# list courses under these paths.
HUB_PAGE_SPECIFICITY = 0.82
SHALLOW_PATH_SPECIFICITY = 0.90


def url_specificity(url: str) -> float:
    """Multiplier reflecting how course-page-like a Candidate's URL is.

    1.0 for a leaf under a level path ("/undergraduate/<slug>"), less for a hub
    or a suspiciously shallow path. Applied by `score_all` in
    `pipeline/match.py` *after* `normalize.score()` has returned, so the number
    that module produces is not the final one.
    """
    path = urllib.parse.urlsplit(url).path
    if _LEVEL_PATH.search(path):
        return 1.0
    if _HUB_PATH.search(path):
        return HUB_PAGE_SPECIFICITY
    depth = len([s for s in path.split("/") if s])
    return 1.0 if depth >= 2 else SHALLOW_PATH_SPECIFICITY


def looks_like_course_name(text: str) -> bool:
    """Is this anchor text plausibly a course name rather than navigation?"""
    if not text:
        return False
    low = text.strip().lower().rstrip(" .›»>|")
    if low in NAV_STOPLIST or len(low) < 3:
        return False
    if any(frag in low for frag in NAV_SUBSTRINGS):
        return False
    if re.fullmatch(r"[\d\W]+", low):          # page numbers, arrows
        return False
    if len(low) > 160:
        return False
    # An award token or two-plus words is enough; bare credential names like
    # "MBA" and "IELTS" are short but legitimate, so they are kept via the
    # award/credential check rather than a word count.
    if len(low.split()) >= 2:
        return True
    return bool(normalize_name(text) and re.fullmatch(r"[a-z]{2,12}", low))


def _is_within(url: str, domain: str) -> bool:
    return registrable(urllib.parse.urlsplit(url).netloc) == domain


# --------------------------------------------------------------------------
# Ladder step 1: hub probing + bounded BFS
# --------------------------------------------------------------------------
def find_seeds(fetcher: Fetcher, website: str,
               ) -> tuple[list[str], list[str], dict[int, int]]:
    """Locate an Institution's course listing roots.

    Returns (seeds, notes, hub_status_counts). The status histogram is the
    evidence `classify_probes` needs; discarding it, as this used to, made a
    site that refuses the crawler indistinguishable from one whose paths we
    simply guessed wrong.
    """
    seeds: list[str] = []
    notes: list[str] = []
    hub_statuses: dict[int, int] = {}
    parts = urllib.parse.urlsplit(website)
    host = parts.netloc
    base = f"{parts.scheme or 'https'}://{host}"
    apex = re.sub(r"^www\.", "", host)

    def accept(final_url: str) -> bool:
        """Reject a seed that is a sign-in wall rather than a listing."""
        if _AUTH_PATH.search(urllib.parse.urlsplit(final_url).path):
            notes.append(f"rejected sign-in page as seed: {final_url}")
            return False
        return True

    for path in HUB_PATHS:
        res = fetcher.get(base + path)
        if res.status is not None:
            hub_statuses[res.status] = hub_statuses.get(res.status, 0) + 1
        if res.ok:
            final = clean_url(res.final_url)
            if final and final not in seeds and accept(final):
                seeds.append(final)
                if registrable(urllib.parse.urlsplit(final).netloc) != \
                        registrable(host):
                    notes.append(f"{path} redirected off-host to {final}")

    for sub in HUB_SUBDOMAINS:
        res = fetcher.get(f"https://{sub}.{apex}/")
        if res.ok:
            final = clean_url(res.final_url)
            if final and final not in seeds and accept(final):
                seeds.append(final)
                notes.append(f"course subdomain {sub}.{apex} responded")
    return seeds, notes, hub_statuses


def crawl_listings(fetcher: Fetcher, seeds: list[str], domains: set[str],
                   max_pages: int = MAX_PAGES_PER_INSTITUTION,
                   ) -> tuple[list[Candidate], int, dict[str, int]]:
    """Bounded BFS over listing pages, harvesting (name, URL) Candidates.

    Returns (candidates, pages_fetched, seed_yield). Each page carries the seed
    it descended from, so a seed that produced nothing can be reported as such
    — that is what exposes a useless probe without having to classify what kind
    of useless it is.
    """
    seen_pages: set[str] = set()
    by_url: dict[str, Candidate] = {}
    yield_by_seed: dict[str, int] = {s: 0 for s in seeds}
    fetched = 0

    # A deterministic priority queue rather than a FIFO. Ordering by
    # (priority, depth, url) makes the crawl reproducible — with a FIFO, the
    # subset of pages that fitted inside the budget depended on discovery
    # order, and the same Institution produced 692 Candidates on one run and
    # 493 on the next. Priority 0 is an explicit course index, 1 a page whose
    # path looks like a listing, 2 everything else.
    # The fourth element is the originating seed. Appending it leaves the
    # existing (priority, depth, url) ordering untouched, so the crawl stays
    # deterministic.
    queue: list[tuple[int, int, str, str]] = [(1, 0, s, s) for s in seeds]
    heapq.heapify(queue)

    def priority_of(url: str, is_index: bool) -> int:
        """Crawl order: 0 an explicit index, 1 a listing-looking path, 2 rest.

        A heuristic for spending the page budget well, never a filter — every
        queued page is eventually fetched if budget allows. `_LISTING_HINT` is
        imprecise on purpose and matches plenty of course pages, which is
        harmless because those are productive to crawl anyway.
        """
        if is_index:
            return 0
        return 1 if _LISTING_HINT.search(urllib.parse.urlsplit(url).path) else 2

    while queue and fetched < max_pages:
        _prio, depth, url, origin = heapq.heappop(queue)
        url = clean_url(url)
        if not url or url in seen_pages:
            continue
        seen_pages.add(url)
        res = fetcher.get(url)
        fetched += 1
        if not res.ok:
            continue

        for text, href in extract_links(res.text, res.final_url):
            href = clean_url(href)
            if not href or not any(_is_within(href, d) for d in domains):
                continue

            # A course index is worth crawling regardless of its path, and its
            # children are what we actually want, so it enters at depth 0 with
            # the highest priority.
            is_index = bool(_INDEX_ANCHOR.search(text or ""))
            if is_index and href not in seen_pages:
                heapq.heappush(queue, (0, 0, href, origin))
                continue

            if not _COURSE_PATH.search(urllib.parse.urlsplit(href).path):
                continue
            if looks_like_course_name(text):
                existing = by_url.get(href)
                # Keep the longest anchor text seen for a URL: listing pages
                # often link the same course from a terse card and a fuller
                # heading.
                if existing is None:
                    # Credit the seed on first discovery only; a later, longer
                    # anchor for the same URL refines the name but is not a
                    # second find.
                    yield_by_seed[origin] = yield_by_seed.get(origin, 0) + 1
                if existing is None or len(text) > len(existing.name):
                    by_url[href] = Candidate(
                        name=text, url=href,
                        level=level_from_url(href) or level_of(text))
            if depth < MAX_DEPTH and href not in seen_pages:
                heapq.heappush(queue, (priority_of(href, is_index),
                                       depth + 1, href, origin))
    return list(by_url.values()), fetched, yield_by_seed


# --------------------------------------------------------------------------
# Ladder step 2: sitemaps
# --------------------------------------------------------------------------
def _slug_to_name(url: str) -> str:
    """Best-effort course name from a URL slug, for sitemap-sourced entries.

    Deliberately weak. A slug-derived name is only ever used to *rank* real
    URLs the Site published; it never manufactures a URL (ADR-0001).
    """
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    slug = re.sub(r"[-_+]+", " ", slug)
    slug = re.sub(r"\b(?:degree|course|programme|program)\b", " ", slug,
                  flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", slug).strip()


def harvest_sitemaps(fetcher: Fetcher, website: str, domains: set[str],
                     max_sitemaps: int = 25) -> list[Candidate]:
    """Ladder rung 2: mine course URLs out of the Institution's sitemaps.

    Expands sitemap indexes, keeps only course-looking paths, and derives each
    name from the URL slug — which is why these Candidates are marked
    `source="sitemap"` and are barred from reaching `verified`.

    Weak in practice: sitemaps are frequently absent (Abertay has none),
    trivial (Bond's holds 13 entries and no courses), or full of pages
    indistinguishable from courses by path alone.
    """
    parts = urllib.parse.urlsplit(website)
    base = f"{parts.scheme or 'https'}://{parts.netloc}"
    todo = list(fetcher.sitemaps_from_robots(base)) or []
    todo += [base + "/sitemap.xml", base + "/sitemap_index.xml"]
    seen: set[str] = set()
    out: dict[str, Candidate] = {}
    processed = 0

    while todo and processed < max_sitemaps:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        res = fetcher.get(sm)
        processed += 1
        if not res.ok:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res.text)
        # A sitemap index points at more sitemaps; expand it.
        if "<sitemapindex" in res.text.lower():
            todo.extend(locs)
            continue
        for loc in locs:
            loc = clean_url(loc)
            if not loc or not any(_is_within(loc, d) for d in domains):
                continue
            path = urllib.parse.urlsplit(loc).path
            if not _COURSE_PATH.search(path):
                continue
            name = _slug_to_name(loc)
            if not looks_like_course_name(name):
                continue
            out.setdefault(loc, Candidate(name=name, url=loc,
                                          level=level_from_url(loc),
                                          source="sitemap"))
    return list(out.values())


# --------------------------------------------------------------------------
# Ladder step 3: the JSON endpoint behind a JavaScript course finder
# --------------------------------------------------------------------------
_JSON_HINT = re.compile(
    r"[\"'](/(?:api|rest|data|services|umbraco|sitecore|wp-json)/[^\"'\s]{4,120})[\"']",
    re.IGNORECASE)

_NAME_KEYS = ("title", "name", "coursename", "course_name", "courseTitle",
              "programName", "displayName", "heading")
_URL_KEYS = ("url", "link", "href", "path", "slug", "courseUrl", "permalink")


def _walk_json(node, base_url: str, out: dict[str, Candidate]) -> None:
    if isinstance(node, dict):
        name = next((str(node[k]) for k in _NAME_KEYS
                     if k in node and isinstance(node[k], (str, int))), "")
        href = next((str(node[k]) for k in _URL_KEYS
                     if k in node and isinstance(node[k], str)), "")
        if name and href:
            url = clean_url(urllib.parse.urljoin(base_url, href))
            if url and looks_like_course_name(name):
                out.setdefault(url, Candidate(name=name.strip(), url=url,
                                              level=level_from_url(url)
                                              or level_of(name),
                                              source="json"))
        for v in node.values():
            _walk_json(v, base_url, out)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, base_url, out)


def harvest_json_endpoints(fetcher: Fetcher, seeds: list[str],
                           max_endpoints: int = 12) -> list[Candidate]:
    """Read the API a JavaScript course finder talks to.

    Abertay's course-search page is 148KB of JavaScript yielding 13 nav links;
    the courses themselves come from an endpoint the page references.
    """
    out: dict[str, Candidate] = {}
    tried: set[str] = set()
    for seed in seeds[:6]:
        res = fetcher.get(seed)
        if not res.ok:
            continue
        for path in _JSON_HINT.findall(res.text):
            if len(tried) >= max_endpoints:
                break
            endpoint = clean_url(urllib.parse.urljoin(res.final_url, path))
            if not endpoint or endpoint in tried:
                continue
            tried.add(endpoint)
            api = fetcher.get(endpoint)
            if not api.ok:
                continue
            try:
                data = json.loads(api.text)
            except ValueError:
                continue
            _walk_json(data, api.final_url, out)
    return list(out.values())


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------
def build_catalog(fetcher: Fetcher, institution: str, website: str,
                  expected_rows: int) -> Catalog:
    """Walk the ladder until Extraction Health passes."""
    cat = Catalog(institution=institution)
    if not website:
        cat.strategy = "none"
        cat.failure_reason = "no_website"
        cat.notes.append("no usable website")
        return cat

    seeds, notes, hub_statuses = find_seeds(fetcher, website)
    cat.seeds = seeds
    cat.notes.extend(notes)
    access = classify_probes(hub_statuses)
    if access:
        cat.notes.append(
            f"hub probe statuses: "
            f"{', '.join(f'{k}x{v}' for k, v in sorted(hub_statuses.items()))}")
    if not seeds:
        cat.strategy = "none"
        # "blocked" and "no_hub" are different problems with different fixes:
        # one is the site refusing us, the other is us guessing wrong paths.
        cat.failure_reason = access or "no_hub"
        cat.notes.append(
            "site refused every hub probe" if access == "blocked"
            else "no hub path responded")
        return cat

    domains = {registrable(urllib.parse.urlsplit(s).netloc) for s in seeds}
    domains.add(registrable(urllib.parse.urlsplit(website).netloc))
    domains.discard("")
    cat.domains = sorted(domains)

    found, fetched, seed_yield = crawl_listings(fetcher, seeds, domains)
    cat.candidates = found
    cat.pages_fetched += fetched
    cat.seed_yield = seed_yield
    cat.strategy = "listing"
    if cat.healthy(expected_rows):
        return cat

    sm = harvest_sitemaps(fetcher, website, domains)
    if sm:
        cat.notes.append(f"sitemap contributed {len(sm)} candidates")
        before = len(cat.candidates)
        cat.candidates = _merge(cat.candidates, sm)
        cat.seed_yield["(sitemap)"] = len(cat.candidates) - before
        cat.strategy = "listing+sitemap"
        if cat.healthy(expected_rows):
            return cat

    js = harvest_json_endpoints(fetcher, seeds)
    if js:
        cat.notes.append(f"json endpoint contributed {len(js)} candidates")
        before = len(cat.candidates)
        cat.candidates = _merge(cat.candidates, js)
        cat.seed_yield["(json)"] = len(cat.candidates) - before
        cat.strategy = cat.strategy + "+json"

    if not cat.healthy(expected_rows):
        # Reachable, but extraction came up short. "no_candidates" usually
        # means a JavaScript course finder that publishes nothing in its HTML;
        # "thin" means we found real listings but not enough of them.
        # An access refusal outranks any downstream symptom. ACU answers 403
        # to all 16 hub paths, yet an incidental `catalogue.` subdomain (its
        # *library*, not its course catalog) responds 200 — so it acquires a
        # seed and would otherwise be filed as "no_candidates", hiding the
        # fact that the site refuses us and no crawling will ever reach it.
        cat.failure_reason = access or (
            "no_candidates" if len(cat.candidates) < MIN_CANDIDATES else "thin")
        cat.notes.append(
            f"extraction health failed: {len(cat.candidates)} candidates "
            f"for {expected_rows} rows")
    return cat


def _merge(a: list[Candidate], b: list[Candidate]) -> list[Candidate]:
    by_url = {c.url: c for c in a}
    for c in b:
        cur = by_url.get(c.url)
        if cur is None or len(c.name) > len(cur.name):
            by_url[c.url] = c
    return list(by_url.values())
