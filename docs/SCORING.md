# Scoring and Assignment

How a Course Row ends up with a URL and a number beside it.

The authoritative descriptions live in the code — `pipeline/normalize.py`'s
module docstring for the formula, `pipeline/match.py`'s for Assignment. This
document carries what a docstring holds badly: worked arithmetic, the evidence
behind each tuned value, and the decision tree.

For a live trace of real rows through every stage, see
[WALKTHROUGH.md](./WALKTHROUGH.md). For the thresholds and how to fit them, see
[CALIBRATION.md](./CALIBRATION.md).

## The shape of it

A Score is **one similarity measurement, then a chain of multipliers.**

```
        strip Institution name, drop Awards, strip codes/durations/stopwords
                                  |
        seq (SequenceMatcher)   tok (Jaccard)   cov (containment)
                                  |
              base = 0.65·seq + 0.35·tok
                                  |
         one branch only:  terse name?  -> max(base, 0.55·cov + 0.45·tok)
                           otherwise    -> base × qualifier penalty
                                  |
                          × Award agreement
                                  |
                          × Level agreement
                                  |
                       round to 4dp, cap at 1.0     <- normalize.score() ends here
                                  |
                          × url_specificity          <- applied in match.py
```

That last step is the one people miss. `normalize.score()` knows nothing about
URLs; `score_all()` in `pipeline/match.py` multiplies its result by
`url_specificity()` from `pipeline/catalog.py`. **The number `score()` returns
is not the number used.**

### Course codes are noise, and the shapes vary

The first box in the diagram strips provider course codes. They almost always
appear on the *site's* side of the comparison and not in the sheet's course
name, so an unstripped code is pure noise dragging a correct match down.

Two families, recognised two different ways:

| family | examples | how it is recognised |
|---|---|---|
| UCAS / provider codes | `7G73`, `Q300`, `142L`, `C801`, `D406D` | shape varies too much for a regex: any alphanumeric token of 3–7 characters carrying at least two digits |
| Australian VET codes | `BSB50420`, `SIT50422`, `CHC33021` — and reversed, `22627VIC`, `10991NAT` | matched exactly, `[a-z]{3}\d{5}` or `\d{5}[a-z]{3}` |

The VET codes are eight characters, so the generic rule (which stops at seven)
let them straight through. On the finished sheet 2,900 rows carried one on one
side only, and **924 of them sat below the confident threshold purely because
of it**:

```
Diploma of Leadership and Management
  vs "BSB50420 Diploma of Leadership and Management"     0.776  ->  1.000
```

Matching the shape rather than widening the length bound is deliberate. Raising
the generic cap to nine characters also swept in long alphanumeric tokens that
were genuine content — 22 rows scored worse. The two patterns cover 6,917 of
the 6,945 eight-character digit-bearing tokens in the sheet, and what they
leave behind (`r1160520`, `bsbb0120`) is noise of no fixed shape.


## Worked arithmetic

Real pairs, real numbers, recomputed from the current code.

### The Award guard doing the entire job

```
Agriculture MAgr   vs  Agriculture (MAg, 4 years)     →  1.0000
Agriculture MAgr   vs  Agriculture (BSc, 3 years)     →  0.4500
```

Both Candidates normalise to the subject `agriculture`. Character-for-character
identical, so text similarity is 1.000 for both. The entire difference is
`AWARD_CLASS_MISMATCH` (×0.45). Without it the row would pick whichever
Candidate happened to sort first.

### Award and Level compounding

```
Equine Science BSc (Hons)  vs  Equine Science (MRes, 1 year)   →  0.2250
```

Subjects identical again (`equine science`), so: `1.000 × 0.45` (BSc vs MRes)
`× 0.50` (ug row, pg Candidate) = 0.225. This is the ADR-0001 trap defused —
`/undergraduate/equine-science/` returns 404 while `/postgraduate/equine-science/`
returns 200, so a pattern-generator would have filed the postgraduate page
against this undergraduate row.

### Near-miss variants, which is why the qualifier penalty exists

```
Data Science BSc (Hons)  vs  Data Science (BSc, 3 years)              →  1.0000
Data Science BSc (Hons)  vs  Data Science (…integrated year…) (BSc)   →  0.3759
```

A margin of +0.62 between a course and its own placement-year variant. These
two are separate rows in the CSV with separate `id`s, and getting them the
wrong way round is the single most likely silent error in the whole pipeline.

### Superset Candidates

```
Agriculture BSc (Hons)  vs  Agriculture with Animal Science (BSc, 3 years)  →  0.7000
```

Contains every word the row has, plus two it does not. Scores well below an
exact match but stays above the floor, so it reaches review rather than being
discarded — correct, since sometimes it *is* the right page.

### Terse names

```
MBA  vs  MBA Master of Business Administration  →  0.6625
```

Symmetric similarity scores this 0.193 because of the length asymmetry. The
containment branch recognises it instead. Note it lands in review, not
`verified`: containment is suggestive, never confident.

### Unrelated subjects

```
Equine Science BSc (Hons)  vs  Environmental Science (BSc, 4 years)  →  0.5286
```

Below the 0.55 floor, so nothing is filled. Aberystwyth has withdrawn Equine
Science BSc, and `no_match` is the right answer.

## The tuned values

All in `pipeline/normalize.py` except the last two, which are in
`pipeline/catalog.py` because only that module knows what a URL path means.

| constant | value | why this value |
|---|---|---|
| `SEQ_WEIGHT` / `TOKEN_WEIGHT` | 0.65 / 0.35 | sequence similarity notices word order and partial words; token overlap is the corrective that stops a shared prefix dominating |
| `TERSE_COVERAGE_WEIGHT` / `TERSE_TOKEN_WEIGHT` | 0.55 / 0.45 | `MBA` vs its full title scored 0.193 symmetric, 0.663 by containment |
| `QUALIFIER_PENALTY_PER_TOKEN` | 0.06 | `Agriculture BSc (Hons)` tied at exactly 1.000 against both `Agriculture` and `Agriculture (Top-Up)` without it |
| `QUALIFIER_PENALTY_FLOOR` | 0.72 | stops a long official title losing to a terse wrong one |
| `AWARD_CLASS_MISMATCH` | 0.45 | BSc vs MSc — a different kind of qualification |
| `AWARD_SIBLING_MISMATCH` | 0.62 | BSc vs BA — same class, still a real distinction, but milder |
| `AWARD_CLASS_ONLY_MISMATCH` | 0.50 | classes disagree with no shared postnominal to compare |
| `LEVEL_MISMATCH` | 0.50 | weaker than the Award guard because Level is inferred, and integrated masters legitimately sit under `/undergraduate/` |
| `INSTITUTION_SEGMENT_SHARE` | 0.5 | leaving the Institution name in a title depresses a correct match by ~0.2 |
| `HUB_PAGE_SPECIFICITY` | 0.82 | a subject hub carries exactly the subject's name, so it ties with the real course page on text alone |
| `SHALLOW_PATH_SPECIFICITY` | 0.90 | a one-segment path is rarely a specific course |

The **thresholds** — `CONFIDENT` 0.80, `FLOOR` 0.55, `MIN_MARGIN` 0.10,
`PREFILTER_MIN` 0.30 — live in `pipeline/match.py`. They are reasoned rather
than fitted; [CALIBRATION.md](./CALIBRATION.md) explains how to fit them and
which guards must not be relaxed to buy coverage.

## Assignment

Scoring ranks Candidates for one row. Assignment decides who actually gets
what, **under the constraint that each URL may be claimed once per Institution**
(ADR-0002).

All `(score, row, candidate)` triples are sorted by descending score — ties
broken by row then candidate id, so reruns are byte-identical — and walked
greedily. A pair is taken when the row is unclaimed, the URL is unclaimed and
the score clears the floor.

The consequence people find surprising: **a row can score 1.000 and still get
nothing**, because a stronger row claimed that URL first. Those rows carry
`url_claimed_by_stronger_match`. In the last 12-institution run, 618 rows ended
blank this way — they are variants the Institution does not publish as separate
pages, so a search API would not resolve them either.

Greedy rather than optimal bipartite matching is deliberate: uniqueness holds
under either, and "a stronger row took it" is a sentence a reviewer can act on.

### Margin means two different things

| case | Margin is | reaches the CSV? |
|---|---|---|
| row was assigned a Candidate | assigned score − best **rejected** score | yes |
| row was assigned nothing | its own top-1 − top-2 | **no** — blanked |

A **negative** Margin is meaningful, not a bug: the row was displaced onto a
second choice. Those carry `displaced_took_lower_candidate` and sort to the top
of the Review Queue, because they are the least trustworthy fills in the file.

### From Score to `matched_status`

```
score < FLOOR (0.55)                        ->  no_match     (nothing filled)
score >= CONFIDENT and margin >= MIN_MARGIN ->  confident
margin < MIN_MARGIN                         ->  ambiguous
otherwise                                   ->  probable
                     |
        then, regardless of the above:
        hub-page URL      ->  confident becomes probable, flag hub_page_match
        sitemap-sourced   ->  confident becomes probable, flag name_from_slug
                     |
        then, in run.py's Verification pass:
        live page agrees      ->  confident becomes verified
        live page disagrees   ->  confident becomes probable
        URL does not respond  ->  url_dead
```

Two things worth noting. The demotions run **after** the visible if/elif chain,
so reading that chain alone will mislead you — they are claims about *evidence
quality* rather than about similarity, which is why they are not folded into
the Score. And `verified` is not earned in `match.py` at all; it is earned in
`run.py` when the live page's own `<h1>` agrees with the listing anchor text
that produced the Candidate. Two independent sources agreeing is the whole
claim.

## Gotchas

- **`normalize.score()` is not the final Score.** `url_specificity` is applied
  afterwards, in `match.py`.
- **Margin can be negative**, and that is informative.
- **`_assert_unique_urls` runs before `_drop_offsite_urls`**, so the uniqueness
  assertion sees URLs that may subsequently be dropped. The order is safe —
  dropping only ever removes URLs — but it matters if you add a third check.
- **A perfect Score is not a confident match.** 1.000 against two Candidates is
  an unresolved choice; that is exactly what Margin exists to express, and why
  the two numbers are never multiplied together.
- **One page under two URLs still reads as two Candidates.** A site that serves
  the same page at `/course/17738` and `/course/17738/diploma-of-nursing` gives
  both the same name and the same Score, so the Margin is zero and the row is
  `ambiguous`. Candidate identity was aligned across *names* (see the Catalog
  stage of the walkthrough) but not across URL spellings, which extraction
  cannot detect without fetching both. Roughly 1% of rows in a spot check.

---

## When two Course Rows may share one URL

Scoring decides *which* Candidate a row wants. A separate rule decides whether it
may *have* it when another row already does. See
[ADR-0004](./adr/0004-bounded-url-sharing.md) for the decision; this is the
arithmetic.

### The test

`normalize.are_variant_siblings(a, b)` is true when both hold:

1. `variant_stem(a) == variant_stem(b)`
2. their Award classes intersect, or one of them has no Award at all

`variant_stem()` is `normalize_name(name, drop_awards=True)` with delivery
qualifiers then removed — `placement`, `placement year`, `sandwich`, `top up`,
`foundation year`, `study abroad`, `year abroad`, `professional experience`,
`industrial experience`, `blended learning`, `work experience`, `honours`.

Multi-word qualifiers are stripped **before** their single-word components,
because otherwise `(with Placement Year)` loses `placement` and keeps a stray
`year`:

| name | stem |
|---|---|
| `Anthropology BA (Hons)` | `anthropology` |
| `Anthropology with Placement BA (Hons)` | `anthropology` |
| `Archaeology and Anthropology BA (Hons)` | `archaeology anthropology` |
| `LLM Master of Laws (with Placement Year)` | `master laws` |

### Why a stem test rather than a threshold

Because Score cannot do it. Both of these pairs score **0.775**:

```
Anthropology BA  vs  Anthropology with Placement BA    must share
Anthropology BA  vs  Archaeology and Anthropology BA   must not
```

Any floor admitting the first admits the second. The stems differ, so the stem
test separates them.

### Why the Award class must also agree

A page holding a BSc and an MSc of one subject is a multi-course page, not one
course in two variants. `Mathematics BSc (Hons)` and `Mathematics MSc` share the
stem `mathematics`, and only the Award check keeps them apart.

### The cap

`SHARE_CAP = 8`. Legitimate Share Groups in the data are almost all 2–3 — the
largest observed on a real run was 4. The cap is a backstop against a stem
collision the test does not foresee, not the primary guard: the pathological
collapse it exists to prevent ran to 116 rows and is already rejected by the
floor, at 0.130–0.141.

### What happens on refusal

The row stays **blank**, carrying `share_denied_stem_mismatch` (or
`share_denied_cap_reached`), `denied_url=<url>` and `denied_held_by=<course_id>`.

Filling it anyway would raise coverage and destroy the only interpretive
guarantee sharing has: that a Share Group is one course in several variants. A
reviewer could then no longer tell a legitimate group from a subject collision
without re-reading every group by hand.
