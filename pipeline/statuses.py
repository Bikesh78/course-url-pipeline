"""The `matched_status` vocabulary, in one place.

Why this module exists
----------------------
Two reasons, one structural and one editorial.

**The structural one.** `pipeline/search.py` imports from `pipeline/triage.py`
(the share index, the gate, `classify_change`), so triage cannot import
`SEARCH_FOUND` back without a cycle — and it needs to, because triage must know
not to trade away a URL that search already decided. A neutral module both can
import breaks the knot permanently rather than by careful ordering.

**The editorial one.** These strings are the contract between the pipeline and
whoever reads the CSV. They were spread across three modules, and
`report.phase_for` had to import from two of them to build a single tuple. One
home means a status can be renamed in one place, and the phase mapping cannot
quietly drift from the vocabulary it describes.

What each value means
---------------------
Phase 1, from extraction against the Institution's own Site:

* `verified`   — a Candidate matched *and* the live page confirmed the name
* `probable`   — a good match, weaker live evidence
* `ambiguous`  — the Margin between the top two Candidates was too thin
* `url_dead`   — we assigned a URL and verification found it broken
* `no_match`   — the Site was read, nothing cleared the floor
* `no_catalog` — the Site could not be read at all

Phase 2, which never crawls:

* `carried_over` — the delivered URL came from the source sheet, adopted by
  triage over ours because it scored better against the course name
* `search_found` — the delivered URL came from a search result: scored,
  domain-checked and sharing-checked, but never fetched, so never `verified`
"""

from __future__ import annotations

# ------------------------------------------------------------------- phase 1
CONFIDENT = "confident"      # internal; written to CSV as `verified`
VERIFIED = "verified"
PROBABLE = "probable"
AMBIGUOUS = "ambiguous"
URL_DEAD = "url_dead"
NO_MATCH = "no_match"
NO_CATALOG = "no_catalog"

# ------------------------------------------------------------------- phase 2
CARRIED_OVER = "carried_over"
SEARCH_FOUND = "search_found"

# Neither phase produced this one: a person did, via `manual_urls.csv`. Some
# rows cannot be answered by any signal the pipeline has -- a school year band
# whose only relevant page is the section page covering it -- and a human
# decision needs somewhere to live that survives a re-run. See ADR-0010.
MANUALLY_ASSIGNED = "manually_assigned"

# A URL bearing one of these came from phase 2 rather than from extraction.
# `report.phase_for` reads exactly this, so adding a phase-2 status here is the
# only edit needed to classify it.
# `MANUALLY_ASSIGNED` is deliberately absent: it is not a phase, and
# `phase_for` reports it as `manual`.
PHASE_2_STATUSES = (CARRIED_OVER, SEARCH_FOUND)

# Statuses whose URL triage will give up in favour of a better-scoring prior.
#
# `verified` is absent deliberately: it beat the prior 8:1 where the prior was
# `low_confidence`, and was ahead even against `matched`. `probable` is absent
# because swapping it was a coin flip across 831 disagreements (334 to 311), so
# trading would lose as often as it won.
#
# `search_found` is absent for a different reason. It is a phase-2 decision
# that has already passed the gate, the domain check and the sharing rule, and
# triage holds no evidence that would overrule it — it would simply see an
# empty phase-1 answer, conclude "we have nothing" and adopt a prior instead.
# 6 of the 131 rows in the first search trial had a gate-clearing prior, so
# leaving it out of this tuple is what stops a re-run undoing paid work.
WEAK_STATUSES = (AMBIGUOUS,)

# Statuses triage must leave completely alone, for the reason stated just
# above: their phase-1 answer is empty, so triage would read "we have nothing"
# and adopt a sheet URL over a decision better than its own. 112 of the 751
# unfilled year-band rows have a gate-clearing prior waiting to do exactly
# that, which is why a hand edit to the output file is no substitute for an
# overlay -- see ADR-0010.
NEVER_GIVEN_UP = (SEARCH_FOUND, MANUALLY_ASSIGNED)

# Prior labels from the sheet that triage will swap *towards*. `low_confidence`
# is excluded: our `ambiguous` beat it 1,321 to 1,105.
TRUSTED_PRIOR = ("matched",)
