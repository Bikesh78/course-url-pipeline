"""Scoring Course Rows against a Catalog, and Assignment under uniqueness.

Assignment is per-Institution and enforces that each URL is claimed by at most
one Course Row — see ADR-0002 for why independent per-row matching is unsafe on
this data.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import urllib.parse

from pipeline.catalog import Candidate, Catalog, url_specificity
from pipeline.fetch import registrable
from pipeline.load import CourseRow
from pipeline.normalize import normalize_name, score as score_pair

# Fitted against the observed Margins; see docs/CALIBRATION.md.
CONFIDENT = 0.80        # at or above this (with Margin) a match may auto-fill
FLOOR = 0.55            # below this nothing is filled at all
MIN_MARGIN = 0.10       # a Margin thinner than this is Ambiguous
PREFILTER_MIN = 0.30    # below this a pair is not worth ranking


@dataclass
class Thresholds:
    confident: float = CONFIDENT
    floor: float = FLOOR
    min_margin: float = MIN_MARGIN


@dataclass
class MatchResult:
    row: CourseRow
    candidate: Candidate | None = None
    score: float = 0.0
    margin: float = 0.0
    runner_up: Candidate | None = None
    runner_up_score: float = 0.0
    status: str = "no_match"
    # Score of the row name against the *live page's* own title, set during
    # Verification. Kept as a field rather than a flag so it does not swamp the
    # flag histogram in the coverage report.
    live_score: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self.candidate.url if self.candidate else ""

    @property
    def evidence(self) -> str:
        if not self.candidate:
            return ""
        parts = [f"matched: {self.candidate.name}"]
        if self.live_score is not None:
            parts.append(f"live page title score: {self.live_score:.3f}")
        if self.runner_up is not None:
            parts.append(f"runner-up: {self.runner_up.name} "
                         f"({self.runner_up_score:.3f}) {self.runner_up.url}")
        return " | ".join(parts)

    @property
    def needs_review(self) -> bool:
        return self.status in ("probable", "ambiguous")


def _token_index(candidates: list[Candidate]) -> dict[str, set[int]]:
    """Map a token to the Candidates containing it, to avoid all-pairs scoring."""
    idx: dict[str, set[int]] = defaultdict(set)
    for i, c in enumerate(candidates):
        toks = set(normalize_name(c.name, drop_awards=True).split())
        if not toks:
            toks = set(normalize_name(c.name).split())
        for t in toks:
            idx[t].add(i)
    return idx


def score_all(rows: list[CourseRow], catalog: Catalog,
              institution: str = "") -> dict[str, list[tuple[float, int]]]:
    """Score every Course Row against plausible Candidates.

    Candidates sharing no token with the row are skipped: on this data that
    removes the large majority of pairs without discarding any match that could
    have cleared the floor.
    """
    cands = catalog.candidates
    idx = _token_index(cands)
    out: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        toks = set(normalize_name(row.name, drop_awards=True).split())
        if not toks:
            toks = set(normalize_name(row.name).split())
        pool: set[int] = set()
        for t in toks:
            pool |= idx.get(t, set())
        scored: list[tuple[float, int]] = []
        for i in pool:
            s = score_pair(row.name, cands[i].name, institution,
                           candidate_level=cands[i].level)
            s = round(s * url_specificity(cands[i].url), 4)
            if s >= PREFILTER_MIN:
                scored.append((s, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        out[row.id] = scored
    return out


def assign(rows: list[CourseRow], catalog: Catalog, institution: str = "",
           thresholds: Thresholds | None = None) -> list[MatchResult]:
    """Assign at most one Candidate per Course Row, each URL used at most once."""
    th = thresholds or Thresholds()
    cands = catalog.candidates
    scored = score_all(rows, catalog, institution)
    by_id = {r.id: r for r in rows}

    # Every (score, row, candidate) triple, best first. Ties broken
    # deterministically so a rerun produces an identical CSV.
    triples: list[tuple[float, str, int]] = []
    for rid, pairs in scored.items():
        for s, ci in pairs:
            triples.append((s, rid, ci))
    triples.sort(key=lambda t: (-t[0], t[1], t[2]))

    claimed_rows: dict[str, int] = {}
    claimed_urls: set[str] = set()
    for s, rid, ci in triples:
        if rid in claimed_rows or cands[ci].url in claimed_urls:
            continue
        if s < th.floor:
            continue
        claimed_rows[rid] = ci
        claimed_urls.add(cands[ci].url)

    results: list[MatchResult] = []
    for row in rows:
        pairs = scored.get(row.id, [])
        res = MatchResult(row=row)

        # Margin is a property of the row's own ranking over the whole Catalog,
        # independent of what other rows claimed.
        if pairs:
            res.margin = pairs[0][0] - (pairs[1][0] if len(pairs) > 1 else 0.0)

        ci = claimed_rows.get(row.id)
        if ci is None:
            if pairs and pairs[0][0] >= th.floor:
                # Its best Candidate went to a stronger claim.
                res.status = "no_match"
                res.flags.append("url_claimed_by_stronger_match")
                res.runner_up = cands[pairs[0][1]]
                res.runner_up_score = pairs[0][0]
            else:
                res.status = "no_match"
            results.append(res)
            continue

        res.candidate = cands[ci]
        res.score = next(s for s, i in pairs if i == ci)
        # Runner-up is the best Candidate that is not the assigned one.
        for s, i in pairs:
            if i != ci:
                res.runner_up, res.runner_up_score = cands[i], s
                break
        res.margin = res.score - res.runner_up_score

        if res.margin < 0:
            # The row's best Candidate was claimed by a stronger match, so this
            # is a second choice. A negative Margin is the signal for exactly
            # that, and such rows sort to the top of the Review Queue.
            res.flags.append("displaced_took_lower_candidate")

        if res.score >= th.confident and res.margin >= th.min_margin:
            res.status = "confident"
        elif res.margin < th.min_margin:
            res.status = "ambiguous"
        else:
            res.status = "probable"

        if url_specificity(res.candidate.url) < 1.0:
            # A hub or category page is not a course page. It may still be the
            # most useful URL available, but calling it verified would be a
            # false claim, so it is always routed to review.
            res.flags.append("hub_page_match")
            if res.status == "confident":
                res.status = "probable"
        if res.candidate.source == "sitemap":
            # A sitemap-derived name comes from a URL slug, not from text the
            # Site printed next to the course, so it is weaker evidence.
            res.flags.append("name_from_slug")
            if res.status == "confident":
                res.status = "probable"
        results.append(res)

    _assert_unique_urls(results)
    _drop_offsite_urls(results, catalog)
    return results


def _drop_offsite_urls(results: list[MatchResult], catalog: Catalog) -> None:
    """A Course Row may only receive a URL on its own Institution's Site.

    Extraction already restricts crawling to the Institution's domains, so a
    violation means a bug upstream rather than a data condition. The URL is
    dropped rather than trusted: an off-site link is exactly the shape a
    marketing aggregator takes, and filing one would be worse than a blank.
    """
    allowed = set(catalog.domains)
    if not allowed:
        return
    for r in results:
        if not r.url:
            continue
        host = registrable(urllib.parse.urlsplit(r.url).netloc)
        if host not in allowed:
            r.flags.append(f"offsite_url_dropped={host}")
            r.candidate = None
            r.score = 0.0
            r.status = "no_match"


def _assert_unique_urls(results: list[MatchResult]) -> None:
    """The ADR-0002 rail. A violation is a bug, not a data condition."""
    seen: dict[str, str] = {}
    for r in results:
        if not r.url:
            continue
        if r.url in seen:
            r.flags.append("duplicate_url_collision")
            raise AssertionError(
                f"URL assigned twice within {r.row.institution_name!r}: "
                f"{r.url} claimed by rows {seen[r.url]} and {r.row.id}")
        seen[r.url] = r.row.id
