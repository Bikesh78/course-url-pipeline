"""Human decisions, applied over whatever the automated stages concluded.

Why a channel for this exists
-----------------------------
Some rows cannot be answered by any signal the pipeline has. "Secondary Junior
7-10" at A.B. Paterson College is the worked case: no course page exists at
all, and the best available answer is the school's secondary-section page. Its
slug scores 0.429 against the course name and the live page's `<h1>`
("Secondary School") scores the same, so neither the gate nor verification can
promote it. Only a person can decide that page is the right answer.

Why an input file rather than editing the output
------------------------------------------------
`review_queue.csv` has always let a reviewer *see* the candidates, but nothing
read a reviewer's choice back -- it was an output with no return path. Editing
`courses_filled.csv` by hand is not a substitute, measured across the 751
unfilled year-band rows: 112 of them have a prior URL in the source sheet that
triage would adopt on the next run, silently reverting the edit. The other 639
survive only incidentally, and even they end up labelled as though *extraction*
had produced the URL.

An overlay is durable because it is re-applied every run, and honest because
the row it writes says a human decided it, who, and why.

What a decision is allowed to override
--------------------------------------
The gate and the floor, by definition -- that is the point. Also the sharing
rule, because one section page legitimately covers every year band at a school,
and refusing that would make the decision unrecordable. Both overrides are
*flagged* rather than silent: the collapse ADR-0004 normally prevents becomes
deliberate and visible in the data.

What it never claims is `verified`. The pipeline did not fetch the page. If the
person did, that belongs in `note`, and `decided_by` is what makes it
accountable.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field

from pipeline.catalog import clean_url
from pipeline.load import _host_of
from pipeline.search import on_site
from pipeline.statuses import MANUALLY_ASSIGNED
from pipeline.triage import ShareIndex, add_flag, classify_change

DEFAULT_MANUAL_FILE = "manual_urls.csv"

COLUMNS = ("id", "course_url", "note", "decided_by", "decided_at")


@dataclass
class ManualStats:
    """What the overlay did, for the run log and for verification."""

    applied: int = 0
    unknown_ids: list[str] = field(default_factory=list)
    bad_urls: list[str] = field(default_factory=list)
    shared_pages: int = 0
    off_domain: int = 0

    @property
    def rejected(self) -> int:
        """Entries that named nothing the pipeline could act on."""
        return len(self.unknown_ids) + len(self.bad_urls)


def load_manual_urls(path: str = DEFAULT_MANUAL_FILE) -> list[dict]:
    """Read the overlay file, or return nothing when there is none.

    A missing file is the common case and not an error: most checkouts carry no
    human decisions at all.
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("id") or "").strip()]


def apply_manual_urls(rows: list[dict], entries: list[dict],
                      index: ShareIndex | None = None) -> ManualStats:
    """Write each decision onto its row, in place, winning over every stage.

    Applied last, so it overrides triage and search alike -- a person looking at
    the page beats a slug score. `matched_score` and `match_margin` are left
    blank: the gate was overridden, and printing the score it failed beside the
    decision that overruled it would read as justification for it.

    A typo must be loud. An id that matches no row, or a URL that is not http,
    is counted and named by the caller rather than skipped in silence: a
    mistyped id that quietly does nothing is the failure most likely to waste
    someone's afternoon.
    """
    stats = ManualStats()
    by_id = {r["id"]: r for r in rows}

    for entry in entries:
        cid = (entry.get("id") or "").strip()
        url = (entry.get("course_url") or "").strip()
        row = by_id.get(cid)

        if row is None:
            stats.unknown_ids.append(cid)
            continue
        if not url.startswith(("http://", "https://")):
            stats.bad_urls.append(f"{cid}: {url!r}")
            continue

        url = clean_url(url)
        site = _host_of(row.get("website", ""))
        name = row.get("name", "")

        # Both overrides are recorded, never silent. A section page shared
        # across a school's year bands is the expected shape here, so refusing
        # it would make the decision unrecordable -- but the data has to say
        # that the collapse was chosen rather than missed.
        if index is not None and index.would_break(site, url, name):
            stats.shared_pages += 1
            add_flag(row, "share_accepted_by_human")
        if site and not on_site(url, site):
            stats.off_domain += 1
            add_flag(row, "url_off_institution_domain")

        was = (row.get("course_url") or "").strip()
        row["course_url"] = url
        row["matched_status"] = MANUALLY_ASSIGNED
        row["matched_score"] = ""
        row["match_margin"] = ""
        row["match_evidence"] = _evidence(entry)
        add_flag(row, "url_from_human")
        row["url_change"] = classify_change(
            (row.get("prior_course_url") or "").strip(), url)
        if index is not None:
            index.move(site, was, url, cid)
        stats.applied += 1

    return stats


def _evidence(entry: dict) -> str:
    """`match_evidence` for a manual row: who decided, when, and why."""
    who = (entry.get("decided_by") or "").strip() or "unattributed"
    when = (entry.get("decided_at") or "").strip()
    note = (entry.get("note") or "").strip()
    text = f"manually assigned by {who}"
    if when:
        text += f" on {when}"
    return f"{text}: {note}" if note else text
