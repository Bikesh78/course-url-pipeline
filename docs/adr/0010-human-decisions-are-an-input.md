---
status: accepted
---

# A human decision is an input, not an edit to the output

Some rows cannot be answered by any signal this pipeline has, and no amount of
tuning changes that.

The worked case is `Secondary Junior 7-10` at A.B. Paterson College. Manual
searching confirms **no course page exists** — the school publishes a page per
section, not per year band. The best available answer is
`/college-life/secondary-school`, whose slug scores **0.429** against the course
name. Fetching it does not help: its `<h1>` is "Secondary School", which scores
**0.429** too. Both are below the 0.55 floor, and correctly so — that page would
be the closest match for *every* year band at that school, which is exactly the
one-page-many-courses collapse [ADR-0004](./0004-bounded-url-sharing.md)
prevents.

A person can look at it and decide it is right. The pipeline had nowhere to put
that decision.

## Why not simply edit `courses_filled.csv`

Because the edit does not survive, and where it does it lies. Measured across
the 751 unfilled rows whose names are year bands:

| | rows |
|---|---|
| the sheet holds a prior URL that triage adopts on the next run | **112** |
| no prior, so the edit survives — incidentally, not by design | 639 |

Triage decides from `phase1_course_url`, which is empty for these rows, so it
reads "we have nothing" and takes the sheet's URL instead. And in the 639 that
do survive, `matched_status` still says `no_catalog` and the `phase` column
computes to `1` — the file claims *extraction* produced a URL a person chose,
with no record of who, when, or why.

`review_queue.csv` had the same shape of gap from the other direction: it exists
to let a reviewer *see* the candidates, but nothing ever read a reviewer's
choice back. It was an output with no return path.

## Decision

Human decisions live in **`manual_urls.csv`**, tracked in git beside
`final_courses.csv` — the only file here a person authors by hand, and the only
one that is not regenerable:

```csv
id,course_url,note,decided_by,decided_at
```

It is applied as the **last** Phase 2 stage, after triage and search, and it
wins over both. An applied row gets `matched_status = manually_assigned`, the
flag `url_from_human`, and a `match_evidence` naming the decider, the date and
the note. `matched_score` and `match_margin` are left **blank**: the gate was
overridden, and printing the score it failed beside the decision that overruled
it would read as justification for it.

`manually_assigned` is **never `verified`**. The pipeline did not fetch the
page. If the person did, that belongs in `note`, and `decided_by` is what makes
it accountable.

`phase_for` reports these rows as **`manual`** rather than `1` or `2`, because a
human decision is not a phase. Keeping it in the same column preserves the
one-question-one-column property that `phase` exists for.

### Overrides are recorded, never silent

A manual URL may override the sharing rule, and normally will: one section page
covering a school's year bands is the expected shape here, and refusing it would
make the decision unrecordable. `ShareIndex.would_break` is still consulted —
only the verdict changes — and the row carries **`share_accepted_by_human`**. An
off-domain URL applies likewise with **`url_off_institution_domain`**, since a
state curriculum page can legitimately be the right answer.

The collapse ADR-0004 prevents therefore becomes *deliberate and visible in the
data*, rather than silently prevented or silently allowed.

### Mistakes are loud

An id matching no row, or a URL that is not `http(s)`, is counted and named in
the run log while the valid entries still apply. A mistyped id that quietly does
nothing is the failure most likely to waste an afternoon.

## Consequences

`manually_assigned` joins `search_found` in `statuses.NEVER_GIVEN_UP`, so triage
leaves both alone. Being an input is what makes a decision *durable*; that tuple
is what makes it *stable*. Verified on a row from the 112: the human URL holds
through two consecutive runs and the second output is byte-identical, where
before it reverted to `carried_over` on the first.

The overlay is applied by **Phase 2 only**. A Phase 1 re-run rewrites the result
file from the crawl and would drop manual URLs until Phase 2 runs again. Phase 1
→ Phase 2 is the normal order, so this is documented rather than solved:
applying the overlay in two writers would risk them diverging.

We did **not** take the alternative of loosening the gate or the sharing rule
for "section pages" in general. That would apply to every row, including the
ones nobody has reviewed, and would trade a known gap for an unbounded one.
