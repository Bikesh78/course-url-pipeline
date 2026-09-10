"""Decide between our extracted URL and the one the source sheet already had.

Why this stage exists
---------------------
`final_courses.csv` arrives carrying a previous pipeline's `course_url` on
28,835 rows. Phase 1 re-derived every row independently, so for many courses
there are now *two* candidate answers. Choosing between them is worth doing:
measured across the whole sheet, taking the better of the two raises fill from
**46.1% to 51.1%** -- 3,974 adoptions. An earlier estimate of 54.0% predated the
sharing check below, which refuses ~1,974 adoptions that would file one page
against unrelated courses; 51.1% is the measured figure.

Why a quality gate rather than a status rule
--------------------------------------------
An earlier rule was drafted from 32 disagreements in a single chunk — *our
`verified` wins, our `ambiguous`/`probable` loses to a prior `matched`*.
Re-measured against all **9,388 real disagreements**, only one cell of it
survived:

    prior=matched  ours=ambiguous   2949 rows   prior better 1613 : 498  ours
    prior=low_conf ours=ambiguous   2800 rows   ours  better 1321 : 1105 prior
    prior=low_conf ours=probable    1279 rows   ours  better  909 : 293  prior
    prior=matched  ours=probable     831 rows   334 : 311          coin flip
    prior=low_conf ours=verified     676 rows   ours  better  575 : 72   prior
    prior=matched  ours=verified     519 rows   227 : 180          near-even

Applied as drafted it would have flipped ~831 coin-flips the wrong way and
preferred `low_confidence` priors over our own work in roughly 4,000 rows.

So the deciding signal here is not either pipeline's label but a **gate**:
score the URL's slug against the course name, and require our own floor. That
is independent of provenance, and it happens to sort the sheet's own URLs the
way its labels imply — admitting 88% of `matched`, rejecting 80% of
`low_confidence` — which is a good sign the referee is sound rather than
circular.

The gate is a *referee, not an oracle*. A correct URL with an opaque slug
scores badly, so the gate is only ever used to choose between two URLs that
both exist, never to accept one on its own. Ties keep our result.
"""

from __future__ import annotations

import collections
import re
import urllib.parse
from dataclasses import dataclass, field

from pipeline.catalog import _slug_to_name, clean_url
from pipeline.load import _host_of
from pipeline.match import FLOOR, SHARE_CAP
from pipeline.normalize import are_variant_siblings, score
from pipeline.statuses import (CARRIED_OVER, NEVER_GIVEN_UP,
                               PHASE_2_STATUSES, SEARCH_FOUND, TRUSTED_PRIOR,
                               URL_DEAD, WEAK_STATUSES)

# The vocabulary lives in `pipeline.statuses` so that search can import it
# too -- search already imports this module, so the reverse would be a cycle.
# Re-exported here because this is where the adoption rules read.
#
# Our own URL is known broken, so almost any live alternative is better.
DEAD_STATUS = URL_DEAD


@dataclass
class TriageStats:
    """What triage did, for the coverage report and for verification."""

    adopted_blank: int = 0
    adopted_dead: int = 0
    adopted_weak: int = 0
    rejected_by_gate: int = 0
    rejected_by_sharing: int = 0
    kept_ours: int = 0
    changes: collections.Counter = field(default_factory=collections.Counter)

    @property
    def adopted(self) -> int:
        """Total rows whose URL now comes from the source sheet."""
        return self.adopted_blank + self.adopted_dead + self.adopted_weak


def gate_score(name: str, url: str, institution: str) -> float:
    """How well a URL's own slug matches the course name, on 0.0-1.0.

    Deliberately reads only the URL, never the page: triage runs over an
    existing result file with no network, and a signal that needed fetching
    could not be applied to 52,703 rows for free.
    """
    if not url:
        return 0.0
    return score(name, _slug_to_name(url), institution)


def _page_identity(url: str) -> tuple[str, str, str]:
    """A URL reduced to what actually identifies a page, for comparison only.

    Looser than `clean_url`, deliberately and only here. `clean_url` defines
    Candidate identity and feeds the sharing rule, where treating `www.x` and
    `x` as one host would be an assumption about DNS we have no right to make.
    But for *"did this course's page move?"*, a scheme upgrade or a `www.`
    prefix is not a move — 123 rows differed by nothing else and would have
    been reported as changed.
    """
    parts = urllib.parse.urlsplit(clean_url(url))
    host = re.sub(r"^www\.", "", parts.netloc.lower())
    return (host, parts.path, parts.query)


def same_page(a: str, b: str) -> bool:
    """Do two URLs address the same page?

    Trailing slash, fragment, tracking parameters, scheme and a `www.` prefix
    are all not differences.
    """
    return bool(a) and bool(b) and _page_identity(a) == _page_identity(b)


def classify_change(prior: str, final: str) -> str:
    """How the delivered URL differs from the one the sheet arrived with.

    Answers *only* "did the answer change". Where the answer came from is
    `matched_status`; keeping the two questions in separate columns is what
    stops either becoming ambiguous.
    """
    if not prior and not final:
        return "none"
    if not prior:
        return "added"
    if not final:
        return "dropped"
    return "unchanged" if same_page(prior, final) else "changed"


def _sharing_would_break(url: str, name: str, holders: dict[str, list[str]],
                         names: dict[str, str]) -> bool:
    """Would adopting *url* create a Share Group that is not Variant Siblings?

    The source sheet shares URLs across courses at 60%, including one page
    holding 116 unrelated courses. Adopting prior URLs without this check would
    import exactly the collapse ADR-0004 exists to prevent.
    """
    current = holders.get(url)
    if not current:
        return False
    if len(current) >= SHARE_CAP:
        return True
    return not all(are_variant_siblings(name, names[rid]) for rid in current)


def add_flag(row: dict, flag: str) -> None:
    """Append *flag* to `row_flags` unless it is already there.

    Appending unconditionally made phase 2 non-idempotent in a way the
    decision counts did not reveal: a re-run reached the same verdict for
    every row and still produced a different file, because a refusal flag was
    recorded twice. Comparing flags as a *set* hid it, which is why the guard
    lives here rather than in a caller.
    """
    existing = [f for f in (row.get("row_flags") or "").split(";") if f]
    if flag not in existing:
        existing.append(flag)
    row["row_flags"] = ";".join(existing)


def phase1_url(row: dict) -> str:
    """What extraction decided for this row, whatever has happened since.

    Read from `phase1_course_url` when present, falling back to `course_url`
    for a result file written before that column existed — where the two are
    equal by definition, since nothing has yet overwritten it.

    Deciding from this rather than from the delivered column is what makes
    phase 2 idempotent. Reading `course_url` meant "our answer" was whatever
    the *last* run delivered, so re-running triage over its own output adopted
    9 more rows and refused 9 fewer by the sharing rule.
    """
    if "phase1_course_url" in row:
        return (row.get("phase1_course_url") or "").strip()
    return (row.get("course_url") or "").strip()


def phase1_status(row: dict) -> str:
    """The status extraction assigned, paired with `phase1_url`."""
    if "phase1_matched_status" in row:
        return (row.get("phase1_matched_status") or "").strip()
    return (row.get("matched_status") or "").strip()


class ShareIndex:
    """Which URLs are held by which Course Rows, scoped per Site.

    Both adoption stages need this and they need the *same instance*: a URL
    triage carried over is a holder by the time search runs, and letting two
    courses take one search hit would reimport the collapse ADR-0004 exists to
    prevent. Assignment scopes Share Groups per Site, so this does too.
    """

    def __init__(self, rows: list[dict]):
        self.by_site: dict[str, dict[str, list[str]]] = (
            collections.defaultdict(lambda: collections.defaultdict(list)))
        self.names: dict[str, str] = {}
        for r in rows:
            self.names[r["id"]] = r.get("name", "")
            url = phase1_url(r)
            if url:
                self.by_site[_host_of(r.get("website", ""))][url].append(r["id"])

    def would_break(self, site: str, url: str, name: str) -> bool:
        """Would filing *url* against *name* break the sharing rule on *site*?"""
        return _sharing_would_break(url, name, self.by_site[site], self.names)

    def move(self, site: str, old_url: str, new_url: str, rid: str) -> None:
        """Reassign *rid* from *old_url* to *new_url*, so later rows see it."""
        holders = self.by_site[site]
        if old_url and rid in holders.get(old_url, ()):
            holders[old_url].remove(rid)
        holders[new_url].append(rid)


def triage_rows(rows: list[dict], source: dict[str, dict],
                floor: float = FLOOR,
                index: "ShareIndex | None" = None) -> TriageStats:
    """Apply prior-URL triage to output rows in place, and record provenance.

    `rows` are output-CSV dicts; `source` maps course id to the input sheet's
    row. Both the URL decision and the `url_change` column are set here, and in
    that order — provenance describes the *delivered* result, so a row triage
    restores reads `unchanged` rather than `dropped`.

    `index` is the shared `ShareIndex`. Pass the one the search stage will also
    use, so a URL adopted here is a holder there; omitted, one is built for
    this call alone.
    """
    stats = TriageStats()
    if index is None:
        index = ShareIndex(rows)

    for r in rows:
        src = source.get(r["id"], {})
        prior = (src.get("course_url") or "").strip()
        prior_status = (src.get("matched_status") or "").strip()
        ours = phase1_url(r)
        status = phase1_status(r)
        name = r.get("name", "")
        inst = r.get("institution_name", "")

        # A row search or a human already answered is left exactly as it is.
        # Triage decides from phase 1's columns, and for these rows extraction
        # found nothing -- so without this it would read "we have nothing",
        # adopt a prior URL, and silently undo work that cost money or someone
        # an afternoon. 6 of the 131 search adoptions had a gate-clearing
        # prior; so do 112 of the 751 unfilled year-band rows.
        if (r.get("matched_status") or "").strip() in NEVER_GIVEN_UP:
            stats.kept_ours += 1
            r["prior_course_url"] = prior
            r["prior_matched_status"] = prior_status
            r["url_change"] = classify_change(
                prior, (r.get("course_url") or "").strip())
            stats.changes[r["url_change"]] += 1
            continue

        # Canonicalise our own URL too, not just adopted ones. A result file
        # written before `clean_url` learned to strip trailing slashes and
        # whitespace holds URLs in the old shape, and comparing those against
        # canonical prior URLs reports a course as moved when only its slash
        # differs.
        if ours:
            ours = clean_url(ours)
            r["course_url"] = ours

        r["prior_course_url"] = prior
        r["prior_matched_status"] = prior_status

        adopt_reason = None
        if prior:
            g_prior = gate_score(name, prior, inst)
            if g_prior >= floor:
                if not ours:
                    adopt_reason = "blank"
                elif status == DEAD_STATUS:
                    adopt_reason = "dead"
                elif (status in WEAK_STATUSES
                      and prior_status in TRUSTED_PRIOR
                      and not same_page(prior, ours)
                      and g_prior > gate_score(name, ours, inst)):
                    adopt_reason = "weak"
            elif not ours or status == DEAD_STATUS:
                # A prior URL exists but does not describe this course well
                # enough to be worth filing. 6,339 such rows come from the
                # sheet's own `low_confidence` band, measured ~65% wrong.
                stats.rejected_by_gate += 1

        if adopt_reason:
            site = _host_of(r.get("website", ""))
            if index.would_break(site, clean_url(prior), name):
                stats.rejected_by_sharing += 1
                add_flag(r, "adoption_denied_sharing")
                adopt_reason = None

        if adopt_reason:
            canonical = clean_url(prior)
            index.move(_host_of(r.get("website", "")), ours, canonical, r["id"])
            r["course_url"] = canonical
            r["matched_status"] = CARRIED_OVER
            r["matched_score"] = f"{gate_score(name, prior, inst):.4f}"
            r["match_margin"] = ""
            r["match_evidence"] = (
                f"carried from source sheet (prior status: "
                f"{prior_status or 'unknown'})")
            add_flag(r, "url_from_source_sheet")
            setattr(stats, f"adopted_{adopt_reason}",
                    getattr(stats, f"adopted_{adopt_reason}") + 1)
        else:
            stats.kept_ours += 1

        r["url_change"] = classify_change(prior, (r.get("course_url") or "").strip())
        stats.changes[r["url_change"]] += 1

    return stats
