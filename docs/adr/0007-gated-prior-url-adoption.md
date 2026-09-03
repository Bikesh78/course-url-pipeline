---
status: accepted
---

# Choose between our URL and the sheet's with a quality gate, not a status rule

`final_courses.csv` arrives carrying a previous pipeline's `course_url` on
28,835 rows. Phase 1 re-derived every row independently, so for many courses
there are two candidate answers. We decide between them by scoring each URL's
own slug against the course name and requiring our existing floor (0.55) — not
by trusting either pipeline's confidence label.

## The rule we rejected, and why it is worth recording

A rule was first drafted from **32 disagreements in a single chunk**: *our
`verified` wins; our `ambiguous`/`probable` loses to a prior `matched`.* It
looked clean, and the examples supporting it were real.

Re-measured against all **9,388 real disagreements** on the full sheet — using
name-vs-slug similarity, which favours neither pipeline — only one cell of it
survived:

| prior said | ours said | rows | ours better | prior better |
|---|---|---|---|---|
| `matched` | `ambiguous` | 2,949 | 498 | **1,613** |
| `low_confidence` | `ambiguous` | 2,800 | **1,321** | 1,105 |
| `low_confidence` | `probable` | 1,279 | **909** | 293 |
| `matched` | `probable` | 831 | 334 | 311 |
| `low_confidence` | `verified` | 676 | **575** | 72 |
| `matched` | `verified` | 519 | 227 | 180 |

Applied as drafted, it would have flipped roughly 831 coin-flips the wrong way
and preferred `low_confidence` priors over our own results in about 4,000 rows.

The lesson is not that the first rule was careless — it is that **32 samples
cannot support a rule applied to 52,703 rows**, and that the disagreement
matrix, not the anecdotes, is what tells you which cells are real.

## What we do instead

Score the URL's slug against the course name. This is independent of which
pipeline produced it, and it sorts the sheet's own URLs the way its labels
imply without being told them:

| prior status | passes the gate | fails |
|---|---|---|
| `matched` | **2,525** (88%) | 341 |
| `low_confidence` | 1,628 (20%) | **6,339** |

Adoption then happens in three cases only: we have nothing, our URL is dead, or
our `ambiguous` result is beaten by a `matched` prior that also scores higher.
`verified` is never given up; `probable` is never given up, because the
measurement says that swap is a coin flip.

## Consequences

**Fill rises from 46.1% to 51.0%.** The plan predicted 54.0%, which did not
model the sharing check below — the honest number is 51.0%.

**The gate is a referee, not an oracle.** A correct URL with an opaque slug
scores badly, so the gate only ever chooses *between two URLs that both exist*.
It never accepts one on its own, and ties keep our result.

**Adoption must pass the sharing rule, and this is not optional.** The sheet
shares URLs across courses at 60%. Of 5,862 adoption candidates, **1,949 were
refused** because the URL was already held by a course that is not a Variant
Sibling — the sheet had filed the Endodontics page against Orthodontics *and*
Periodontics, and a `bachelor-of-science-advanced` page against a Food and
Agribusiness degree. Adopting without this check would import exactly the
collapse [ADR-0004](./0004-bounded-url-sharing.md) exists to prevent.
