---
status: accepted
---

# Assignment enforces one URL per Course Row within an Institution

Each Course Row could be matched to its best Candidate independently, which is
simpler and embarrassingly parallel. We instead solve Assignment per
Institution with a uniqueness constraint: within one Institution, each URL may
be claimed by at most one Course Row.

The reason is that the dominant failure mode is near-miss Award and variant
collisions, not wild mismatches. Aberystwyth carries both
`Data Science BSc (Hons)` and `Data Science (with integrated year in industry)
BSc (Hons)` as separate rows with separate `id`s. Matched independently, both
rows happily select `/undergraduate/data-science/` and one of them is wrong.
Under a uniqueness constraint, the stronger claim takes the URL and the other
row is forced onto its true match or into the Review Queue — the collision
becomes visible instead of silent.

This also makes Margin meaningful. Because every Candidate is scored against
every Course Row in the Institution, the runner-up is a real competitor rather
than an artefact of one row's search, so a thin Margin genuinely indicates
ambiguity.

## Consequences

Assignment is per-Institution and therefore not parallelisable below the
Institution boundary, and an Institution's whole Catalog must be extracted
before any of its rows can be resolved. Greedy descending-Score assignment is
used rather than optimal bipartite matching, because the Review Queue must
explain *why* a row got its URL and greedy is explainable; uniqueness — the
property that matters — holds under either algorithm.
