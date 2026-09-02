"""Scoring Course Rows against a Catalog, and Assignment under bounded sharing.

Assignment is per-Institution. A URL may be held by several Course Rows, but
only when they are **Variant Siblings** — the same course delivered differently,
such as `Anthropology` and `Anthropology with Placement` — and only up to
`SHARE_CAP`. See ADR-0004, which supersedes the strict-uniqueness rail of
ADR-0002, and `docs/SCORING.md` for the decision tree and worked arithmetic.

Why sharing needs a stem test rather than a threshold
-----------------------------------------------------
Score cannot make this distinction. Measured on real names, `Anthropology BA`
scores **0.775** against both `Anthropology with Placement BA` (a sibling,
which should share) and `Archaeology and Anthropology BA` (a different course,
which must not). Identical scores, opposite correct answers, so no floor or
margin separates them — `normalize.variant_stem()` does.

Sharing is *not* what protects against catastrophic collapse; the floor, the
Award/Level guard and subject-only comparison do. Against the 116-course
collapse found in the source data, this scorer rates the victims 0.130-0.141
against a floor of 0.55.

Where the final Score comes from
--------------------------------
`normalize.score()` measures name similarity but knows nothing about URLs.
`score_all()` here multiplies its result by `catalog.url_specificity()`, so the
number this module works with is **not** the number that module returned: a
`score()` of 1.000 against a subject hub page becomes 0.820. That composition
is easy to miss because it spans two modules.

What `assign()` does, in order
------------------------------
1. Score every Course Row against Candidates sharing at least one token.
2. Flatten to `(score, row, candidate)` triples.
3. Sort by descending score, ties broken by row then candidate id, so a rerun
   produces a byte-identical CSV.
4. Walk the triples greedily, claiming a pair when the row is unclaimed, the
   score clears the floor, and the URL is either unheld or held only by Variant
   Siblings within `SHARE_CAP`. **This loop is the sharing rail of ADR-0004.**
   A refusal is recorded with the URL wanted and the row holding it, so a blank
   cell is always explainable.
5. Build one result per row, computing Margin (see below).
6. Apply demotions.
7. Run the post-checks in order: `_drop_offsite_urls`, `_flag_share_groups`,
   then `_assert_shares_are_justified`. Off-site drops come first so a removed
   row cannot make a valid Share Group look malformed.

Two things are easy to break here
---------------------------------
**Status is not decided by the visible if/elif chain alone.** That chain picks
confident / ambiguous / probable from score and Margin, and then two demotions
may knock a `confident` result down to `probable`: a hub-page URL, and a name
derived from a sitemap slug rather than anchor text. Both are claims about
*evidence quality* rather than about similarity, which is why they are applied
afterwards instead of folded into the Score.

**Margin means two different things**, depending on whether the row was
assigned anything:

  - assigned      -> assigned Score minus the best *rejected* Candidate's.
                     Negative when the row was displaced onto a second choice.
  - not assigned  -> the row's own top-1 minus top-2, which is retained only as
                     a debugging aid. It is never written to the CSV, because
                     `write_filled_csv` blanks Margin whenever there is no
                     Candidate.

Greedy, not optimal
-------------------
Descending-score greedy rather than optimal bipartite matching. The property
that matters — that a Share Group is one course in several variants — holds
under either, and greedy is explainable in the Review Queue: "a stronger row
took it, and it is not your variant" is a sentence a reviewer can act on.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import urllib.parse

from pipeline.catalog import Candidate, Catalog, url_specificity
from pipeline.fetch import registrable
from pipeline.load import CourseRow
from pipeline.normalize import (are_variant_siblings, normalize_name,
                                score as score_pair, variant_stem)

# Fitted against the observed Margins; see docs/CALIBRATION.md.
CONFIDENT = 0.80        # at or above this (with Margin) a match may auto-fill
FLOOR = 0.55            # below this nothing is filled at all
MIN_MARGIN = 0.10       # a Margin thinner than this is Ambiguous
PREFILTER_MIN = 0.30    # below this a pair is not worth ranking

# How many Course Rows may share one URL. Legitimate Share Groups in the data
# are almost all 2-3 (a course plus its Honours or Placement variant); the
# pathological collapse in the source sheet ran to 116 courses on a single
# page. The cap is a backstop behind the Variant Sibling test, not the primary
# guard.
SHARE_CAP = 8

log = logging.getLogger(__name__)


@dataclass
class Thresholds:
    """The three numbers that decide what happens to every Course Row.

    Reasoned from observed separations, **not fitted** — the input sheet holds
    only four usable ground-truth URLs. Until `calibration_sample.csv` is
    labelled, read `matched_score` as a ranking rather than a probability.
    Procedure and the caution about which guards must not be relaxed are in
    `docs/CALIBRATION.md`. Overridable per run via --confident/--floor/
    --min-margin.
    """

    confident: float = CONFIDENT
    floor: float = FLOOR
    min_margin: float = MIN_MARGIN


@dataclass
class MatchResult:
    """What the pipeline decided about one Course Row, and why.

    Confidence is carried as three separate numbers rather than one, because
    they fail independently:

        score       how alike the two names are
        margin      how far ahead of the best rejected Candidate
        live_score  agreement with the live page's own heading

    A perfect `score` with a zero `margin` is an unresolved choice, not a
    confident match — which is why they are never multiplied together.

    A **negative margin** is meaningful, not a bug: the row was displaced, its
    best Candidate having gone to a stronger claim, so it holds a second
    choice. Those rows carry `displaced_took_lower_candidate` and sort to the
    top of the Review Queue.
    """

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
        """The assigned URL, or "" when nothing cleared the floor."""
        return self.candidate.url if self.candidate else ""

    @property
    def evidence(self) -> str:
        """Human-readable justification written to `match_evidence`.

        Names the Candidate matched, the live-page agreement, and the runner-up
        it beat — so a reviewer can judge the decision without re-running
        anything or opening the Catalog.
        """
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
        """Does this row belong in `review_queue.csv`?

        Only the uncertain middle. `verified` is trusted, and `no_match` /
        `no_catalog` give a reviewer nothing to arbitrate — a human cannot
        conjure a URL the Catalog does not contain.
        """
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
    """Assign at most one Candidate per Course Row, with bounded URL sharing.

    A URL may be held by several Course Rows, but only when they are Variant
    Siblings — the same course delivered differently — and only up to
    SHARE_CAP. This is what makes Assignment different from matching each row
    independently, and it is load-bearing: see ADR-0004, which supersedes the
    strict-uniqueness rail of ADR-0002.

    The guarantee the sharing rule buys is interpretive: **a shared URL always
    means one course in several variants.** A row whose best URL is held by a
    non-sibling is left blank with the denial recorded, rather than being given
    a URL that would make Share Groups meaningless.
    """
    th = thresholds or Thresholds()
    cands = catalog.candidates
    scored = score_all(rows, catalog, institution)

    # Every (score, row, candidate) triple, best first. Ties broken
    # deterministically so a rerun produces an identical CSV.
    triples: list[tuple[float, str, int]] = []
    for rid, pairs in scored.items():
        for s, ci in pairs:
            triples.append((s, rid, ci))
    triples.sort(key=lambda t: (-t[0], t[1], t[2]))

    by_id = {r.id: r for r in rows}
    claimed_rows: dict[str, int] = {}
    claimed_urls: dict[str, list[str]] = {}          # url -> holding row ids
    # Highest-scoring refusal per row, so the reviewer sees the URL the row
    # most wanted rather than an arbitrary later one.
    share_denied: dict[str, tuple[str, str, str]] = {}

    for s, rid, ci in triples:
        if rid in claimed_rows or s < th.floor:
            continue
        url = cands[ci].url
        holders = claimed_urls.get(url)
        if holders:
            if len(holders) >= SHARE_CAP:
                share_denied.setdefault(rid, (url, holders[0], "cap_reached"))
                continue
            if not all(are_variant_siblings(by_id[rid].name, by_id[h].name)
                       for h in holders):
                share_denied.setdefault(rid, (url, holders[0], "stem_mismatch"))
                continue
        claimed_rows[rid] = ci
        claimed_urls.setdefault(url, []).append(rid)

    results: list[MatchResult] = []
    for row in rows:
        pairs = scored.get(row.id, [])
        res = MatchResult(row=row)
        ci = claimed_rows.get(row.id)

        if ci is None:
            # Nothing assigned. Margin here is the row's own top-1 minus top-2,
            # a different quantity from the assigned case below, and it is kept
            # only as a debugging aid: write_filled_csv blanks Margin whenever
            # there is no Candidate, so this value never reaches the output.
            if pairs:
                res.margin = pairs[0][0] - (pairs[1][0] if len(pairs) > 1
                                            else 0.0)
            res.status = "no_match"
            if row.id in share_denied:
                # Refused a URL a non-sibling holds. Recording which URL and
                # who holds it is what makes this reviewable in one glance
                # instead of an unexplained empty cell.
                denied_url, holder, reason = share_denied[row.id]
                res.flags.append(f"share_denied_{reason}")
                res.flags.append(f"denied_url={denied_url}")
                res.flags.append(f"denied_held_by={holder}")
                res.runner_up = next((cands[i] for _s, i in pairs
                                      if cands[i].url == denied_url), None)
                res.runner_up_score = next((_s for _s, i in pairs
                                            if cands[i].url == denied_url), 0.0)
            elif pairs and pairs[0][0] >= th.floor:
                # Defensive: every refusal above records a reason, so reaching
                # here means the row was outranked for its own Candidate.
                res.flags.append("url_claimed_by_stronger_match")
                res.runner_up = cands[pairs[0][1]]
                res.runner_up_score = pairs[0][0]
            results.append(res)
            continue

        res.candidate = cands[ci]
        res.score = next(s for s, i in pairs if i == ci)
        # Runner-up is the best Candidate that is not the assigned one.
        for s, i in pairs:
            if i != ci:
                res.runner_up, res.runner_up_score = cands[i], s
                break
        # Margin for an assigned row: how far ahead of the best rejected
        # Candidate. Goes negative when a stronger row claimed this one's first
        # choice and it fell back to a second.
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
        if res.candidate.source == "prior":
            # The name came from the page this URL points at, and Verification
            # re-reads that same page — so agreement is circular, not
            # corroboration. Only a URL also found in a listing can be
            # `verified`.
            res.flags.append("name_from_page")
            if res.status == "confident":
                res.status = "probable"
        if res.candidate.source == "sitemap":
            # A sitemap-derived name comes from a URL slug, not from text the
            # Site printed next to the course, so it is weaker evidence.
            res.flags.append("name_from_slug")
            if res.status == "confident":
                res.status = "probable"
        results.append(res)

    # Off-site URLs are dropped before the Share Group check, so a dropped row
    # cannot make an otherwise-valid group look malformed.
    _drop_offsite_urls(results, catalog)
    _flag_share_groups(results)
    _assert_shares_are_justified(results)
    _log_assignment(results, cands, scored)
    return results


def _log_assignment(results: list[MatchResult], cands: list[Candidate],
                    scored: dict[str, list[tuple[float, int]]]) -> None:
    """Emit `match.done`, plus a per-row `match.row` at DEBUG.

    The sample is what makes a coverage figure checkable: a reviewer can see
    which rows got which URL at what score without opening the CSV.
    """
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    shares: dict[str, int] = {}
    for r in results:
        if r.url:
            shares[r.url] = shares.get(r.url, 0) + 1
    denied = sum(1 for r in results
                 if any(f.startswith("share_denied") for f in r.flags))
    filled = [r for r in results if r.url]

    log.info(f"match: {len(filled)}/{len(results)} filled",
             extra={"event": "match.done", "rows": len(results),
                    "filled": len(filled),
                    "pairs_scored": sum(len(v) for v in scored.values()),
                    "candidates": len(cands),
                    "statuses": statuses,
                    "share_groups": sum(1 for n in shares.values() if n > 1),
                    "rows_sharing": sum(n for n in shares.values() if n > 1),
                    "share_denied": denied,
                    "sample": [{"row": r.row.name[:80],
                                "url": r.url,
                                "score": round(r.score, 3),
                                "margin": round(r.margin, 3),
                                "status": r.status}
                               for r in filled[:10]]})

    for r in results:
        log.debug(f"row {r.status}: {r.row.name[:60]}",
                  extra={"event": "match.row", "course_id": r.row.id,
                         "row": r.row.name[:120], "url": r.url,
                         "score": round(r.score, 4),
                         "margin": round(r.margin, 4), "status": r.status,
                         "flags": r.row.flags + r.flags})


def _flag_share_groups(results: list[MatchResult]) -> None:
    """Mark every row that shares its URL, so Share Groups are visible."""
    holders: dict[str, list[MatchResult]] = {}
    for r in results:
        if r.url:
            holders.setdefault(r.url, []).append(r)
    for url, group in holders.items():
        if len(group) > 1:
            for r in group:
                r.flags.append(f"variant_sibling_share={len(group)}")


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


def _assert_shares_are_justified(results: list[MatchResult]) -> None:
    """The ADR-0004 rail: every Share Group is one course in several variants.

    Sharing itself is legal now, so this no longer raises on a repeated URL.
    It raises when a group's members are *not* Variant Siblings, or when a
    group exceeds SHARE_CAP — either of which means the claim loop let
    something through, i.e. a bug rather than a data condition.
    """
    holders: dict[str, list[MatchResult]] = {}
    for r in results:
        if r.url:
            holders.setdefault(r.url, []).append(r)

    for url, group in holders.items():
        if len(group) == 1:
            continue
        if len(group) > SHARE_CAP:
            raise AssertionError(
                f"Share Group over cap in {group[0].row.institution_name!r}: "
                f"{url} held by {len(group)} rows (cap {SHARE_CAP})")
        stems = {variant_stem(r.row.name) for r in group}
        if len(stems) > 1:
            names = ", ".join(repr(r.row.name) for r in group[:4])
            raise AssertionError(
                f"Share Group with mismatched Variant Stems in "
                f"{group[0].row.institution_name!r}: {url} held by {names} "
                f"-> stems {sorted(stems)}")
