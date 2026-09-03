"""Search fallback for rows neither extraction nor the source sheet could fill.

Where this fits
---------------
After Phase 1 and prior-URL triage, **24,256 rows** still have no URL. Two
thirds of them (14,563) are `no_catalog`: the crawler could not read the site at
all, so no amount of crawl tuning reaches them. A search engine can, because it
has already crawled those sites.

The rule that keeps this safe
-----------------------------
**A search hit is a Candidate, not an answer.** It is scored against the course
name, checked against the Site's own domain, put through the Variant Sibling
sharing rule, and can never be reported `verified` on the strength of a search
ranking. "It was the top result" is not evidence about the course; it is
evidence about the search engine. This is the same discipline ADR-0001 applies
to crawled URLs, for the same reason: a plausible wrong URL is worse than a
blank, because nothing downstream can detect it.

No vendor is chosen
-------------------
The provider is an interface. `NullProvider` is the default and returns
nothing, so the pipeline runs unchanged without a key; `FixtureProvider` reads
canned results so the whole path is testable offline. A real vendor implements
one method and nothing else changes.

Rows deliberately not searched
------------------------------
The 5,008 rows flagged `occupation_code_not_course` are ANZSCO skilled-migration
occupation codes ("... - 411511 (subclass 186)"), not courses. No course page
exists to find, and querying for them would spend real money on a guaranteed
miss.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Protocol

from pipeline.fetch import registrable

# Flags marking rows for which no course page can exist.
UNSEARCHABLE_FLAGS = ("occupation_code_not_course", "year_level_not_course")

MAX_RESULTS = 5


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


def build_query(name: str, institution: str, site: str) -> str:
    """The query string for one course.

    Quotes the course name so the engine treats it as a phrase, and scopes to
    the Institution's own host — a course page on an aggregator is not an
    answer, and the domain check downstream would drop it anyway.
    """
    name = re.sub(r"\s+", " ", (name or "").strip())
    parts = [f'"{name}"' if name else ""]
    if institution:
        parts.append(re.sub(r"\s+", " ", institution.strip()))
    if site:
        parts.append(f"site:{site}")
    return " ".join(p for p in parts if p)


def is_searchable(row: dict) -> bool:
    """Is this row worth spending a paid query on?

    False for rows already filled, and for rows whose flags say no course page
    can exist.
    """
    if (row.get("course_url") or "").strip():
        return False
    flags = row.get("row_flags") or ""
    return not any(f in flags for f in UNSEARCHABLE_FLAGS)


def searchable_rows(rows: list[dict]) -> list[dict]:
    """The subset of *rows* a search vendor would be asked about."""
    return [r for r in rows if is_searchable(r)]


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
