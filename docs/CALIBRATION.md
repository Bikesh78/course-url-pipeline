# Threshold calibration

## Why this document exists

Three numbers decide what the pipeline does with every Course Row:

| Threshold | Default | Meaning |
|---|---|---|
| `CONFIDENT` | 0.80 | at or above this, *with* Margin, a match may be reported `verified` |
| `FLOOR` | 0.55 | below this nothing is filled at all |
| `MIN_MARGIN` | 0.10 | a thinner Margin makes the match `ambiguous` regardless of Score |

They live in `pipeline/match.py` and can be overridden per run with
`--confident`, `--floor` and `--min-margin`.

**These defaults are reasoned, not fitted.** The input sheet contains only four
usable ground-truth URLs, so there is no labelled set in the data to fit
against. Until the sample below is labelled, treat `matched_score` as an
*ordering* signal — good for triaging worst-first — rather than as a calibrated
probability that a URL is correct.

## Where the defaults came from

Measured Score separations on real pages, all with the institution name
stripped and Awards/course codes normalised:

| Comparison | Score |
|---|---|
| `Data Science BSc (Hons)` vs its own page | 1.000 |
| `Data Science BSc (Hons)` vs the integrated-year page | 0.458 |
| `Data Science (…integrated year…) BSc (Hons)` vs its own page | 1.000 |
| `Equine Science BSc (Hons)` vs its BSc page | 1.000 |
| `Equine Science BSc (Hons)` vs the MSc page | 0.450 |
| `Equine Science BSc (Hons)` vs an unrelated BSc page | 0.517 |
| `Agriculture BSc (Hons)` vs a broader "with Animal Science" page | 0.700 |
| `MBA` vs "MBA Master of Business Administration" | 0.662 |

Correct matches cluster at or near 1.000; wrong matches on this data sit
between 0.45 and 0.70. `FLOOR` at 0.55 admits the terse-name cases (`MBA`,
`IELTS`) into review while excluding clear mismatches, and `CONFIDENT` at 0.80
sits in the empty band above every observed wrong match.

`MIN_MARGIN` exists because Score alone is not enough. A row scoring 1.000
against *two* Candidates is not a confident match — it is an unresolved choice.
This is common: institutional catalogues carry `X`, `X (Top-Up)`,
`X (with foundation year)` and `X (with integrated year in industry)` as
separate courses while the CSV names only `X`.

## How to fit them properly

1. Run the pipeline. It writes `calibration_sample.csv` — roughly 150 rows
   sampled across Score bands (0.95–1.00, 0.85–0.95, 0.75–0.85, 0.65–0.75,
   0.55–0.65, and a below-floor band).
2. Fill the `HUMAN_VERDICT_correct_yes_no` column by opening each `course_url`
   and judging whether it is the page for that Course Row. Judge the *page*,
   not the score.
3. Compute precision per band. Set `CONFIDENT` to the lowest band whose
   precision meets your requirement, and `FLOOR` to the lowest band still
   worth a reviewer's time.
4. Record the fitted numbers and the date here, and note the sample size.

The below-floor band matters as much as the others: it measures what is being
*discarded*. If it shows high precision, `FLOOR` is set too high and the
pipeline is throwing away good URLs.

## Fitted values

| Date | Sample size | `CONFIDENT` | `FLOOR` | `MIN_MARGIN` | Notes |
|---|---|---|---|---|---|
| — | — | 0.80 | 0.55 | 0.10 | Initial reasoned defaults; not yet fitted. |

## A caution on re-tuning

Two guards are load-bearing and must not be relaxed to raise coverage:

- **The Award/Level guard**, which stopped an undergraduate row taking a
  postgraduate page (`/undergraduate/equine-science/` 404s while
  `/postgraduate/equine-science/` returns 200).
- **The Variant Stem test** of ADR-0004, which decides whether two rows may
  share a URL. Score cannot substitute for it: `Anthropology BA` scores 0.775
  against both `Anthropology with Placement BA` (must share) and
  `Archaeology and Anthropology BA` (must not).

Coverage bought by weakening either is not coverage; it is silent error. If
coverage must rise, raise it by improving Catalog extraction — the
`no_catalog` Institutions in `coverage_report.md` are where the real headroom
is.
