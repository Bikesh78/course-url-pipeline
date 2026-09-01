# Course URL Pipeline

Fills the `course_url` column of `final_courses.csv` (52,781 Course Rows across
2,548 Institutions, grouped into 2,220 Sites) by extracting each Site's own
course listings and matching Course Rows against them, with a Score and Margin
on every result.

The legacy `processed_courses.csv` is a strict subset — every one of its 10,637
ids appears in the current sheet — and is still readable via `--input` for
comparison runs.

Domain vocabulary is defined in [CONTEXT.md](./CONTEXT.md). The three decisions
that shape the architecture are recorded in [docs/adr/](./docs/adr/) — read
ADR-0001 first if you are wondering why this crawls instead of building URLs
from patterns.

| to understand | read |
|---|---|
| what the words mean | [CONTEXT.md](./CONTEXT.md) |
| why it is built this way | [docs/adr/](./docs/adr/) |
| how a Score is computed and how Assignment decides | [docs/SCORING.md](./docs/SCORING.md) |
| what happens to one row, stage by stage | [docs/WALKTHROUGH.md](./docs/WALKTHROUGH.md) |
| how to fit the thresholds | [docs/CALIBRATION.md](./docs/CALIBRATION.md) |
| how the crawler finds listings | the `pipeline/catalog.py` module docstring |
| when two courses may share a URL | [docs/adr/0004-bounded-url-sharing.md](./docs/adr/0004-bounded-url-sharing.md) |
| why results live in SQLite too | [docs/adr/0005-sqlite-for-run-state-and-url-history.md](./docs/adr/0005-sqlite-for-run-state-and-url-history.md) |
| why crawling is per-site, not per-institution | [docs/adr/0006-the-crawl-unit-is-a-website-not-an-institution.md](./docs/adr/0006-the-crawl-unit-is-a-website-not-an-institution.md) |

## Guarantees

- **The input sheet is never written to.** It is input only.
- **URLs are never constructed**, only selected from Site listings (ADR-0001).
- **A URL shared by two Course Rows always means one course in several
  variants.** Sharing requires a matching Variant Stem and Award class, and is
  capped; anything else is refused and recorded (ADR-0004).
- **No paid API, no LLM, no `pip install`** — stdlib plus the already-present
  `bs4`/`lxml` (ADR-0003).
- **Every stage is resumable.** All HTTP responses are cached under `.cache/`,
  and extracted Catalogs under `catalogs/`. An interrupted run resumes free.
- **Every run is traceable.** A `run_id` stamps a JSONL log in `logs/` and every
  row of `pipeline.db`, which also keeps each Course Row's URL history
  (ADR-0005).

## Requirements

Python 3.10+. `bs4` and `lxml` are used where present and degraded to stdlib
`re` parsing where not.

## Usage

```bash
# Full run over all 2,220 Sites. Resumable: stop it and rerun freely.
python run.py

# One Institution by name (processes its whole Site bucket)
python run.py --institution "Aberystwyth University"

# Adversarial harness: must degrade to no_catalog, not emit garbage
python run.py --institution "Abertay University"

# Plan the work without making any network requests
python run.py --dry-run

# The N largest Institutions (they carry most of the rows)
python run.py --limit 12

# Replay entirely from cache; never touch the network
python run.py --offline

# Skip the live re-fetch of assigned URLs (faster, but nothing reaches `verified`)
python run.py --no-verify

# Re-extract Catalogs instead of reusing catalogs/*.json
python run.py --refresh-catalogs

# Override thresholds (see docs/CALIBRATION.md)
python run.py --confident 0.85 --floor 0.60 --min-margin 0.15
```

Other flags: `--delay` (seconds between requests to one domain, default 1.0),
`--workers` (concurrent Sites, default 8), `--input`, `--out`, `--review-out`,
`--report-out`, `--calibration-out`, `--db` (SQLite path; `--db ""` disables the
store), `--log-dir`, `--verbose` (log per-row decisions).

`--limit N` takes the N largest Sites *by rows that are plausibly courses*, not
by raw row count. The largest bucket in the sheet is 5,083 ANZSCO visa
occupation codes on a government site, which no crawl can resolve; ranking on
raw counts would spend the first slot on guaranteed waste. A full run still
covers every bucket.

### Resuming

There is no stage flag, because the caches make one unnecessary. Two layers
persist between runs:

- `.cache/` — every HTTP response, keyed by URL, including 404s
- `catalogs/<institution>.json` — each extracted Catalog

So an interrupted run is resumed by simply running the same command again: it
re-reads what it already fetched and only issues requests for what it has not
seen. Re-running the Aberystwyth harness after a completed run takes **5
seconds** and 2 network requests. Delete `.cache/` to force a refetch, or pass
`--refresh-catalogs` to re-extract Catalogs while keeping the page cache.

## Outputs

| File | Contents |
|---|---|
| `courses_filled.csv` | all input columns plus `course_url`, `matched_score`, `matched_status`, `match_margin`, `live_page_score`, `match_evidence`, `row_flags` |
| `review_queue.csv` | only rows needing human triage, worst Margin first, both competing Candidates shown side by side |
| `coverage_report.md` | coverage %, Review Queue size, per-Institution Extraction Health — the input to the Phase 2 spend decision |
| `calibration_sample.csv` | ~150 stratified rows for one-time human labelling to fit thresholds |
| `pipeline.db` | run state, per-row results and URL history (ADR-0005) |
| `logs/<run_id>.jsonl` | one JSON object per event, with `site_key`, `candidates`, `diagnosis` and friends as top-level fields |

### `matched_status` values

| Status | Meaning |
|---|---|
| `verified` | came from the Institution's own listing **and** a live fetch confirmed the page title matches above threshold |
| `probable` | matched above the floor but below confident, or Margin too thin — in the Review Queue |
| `ambiguous` | two or more Candidates inside the Margin band |
| `no_match` | Catalog found, nothing scored above the floor |
| `no_catalog` | Extraction Health failed for this Institution — a Phase 2 candidate |
| `url_dead` | assigned URL failed its Verification fetch |

### Reading `match_margin`

Margin is the assigned Candidate's Score minus the best *rejected* Candidate's
Score, and it can be **negative**. A negative Margin means the Course Row was
displaced: it scored higher against a URL that a stronger Course Row claimed
first, so it holds a second choice. Those rows carry
`displaced_took_lower_candidate` and sort to the top of the Review Queue,
because they are the least trustworthy fills in the file.

`live_page_score` is separate evidence: the row name scored against the live
page's own `<h1>`/`<title>`, whereas `matched_score` came from the listing
page's anchor text. Agreement between the two is what `verified` asserts.

### `row_flags` values

Input-quality flags: `k12_institution`, `bare_credential_name`,
`year_level_not_course`, `occupation_code_not_course`, `truncated_name`,
`malformed_row_repaired`, `aggregator_website`, `unusable_website`.

`occupation_code_not_course` marks the 5,014 rows that are ANZSCO skilled-
migration occupations rather than courses — "Aboriginal and Torres Strait
Islander Health Worker - 411511 (subclass 186)". Only 33 of them carry any URL
in the source. Without the flag, 5,000 blanks read as a crawler failure.

Sharing flags (ADR-0004): `variant_sibling_share=<n>` on every row in a Share
Group; `share_denied_stem_mismatch` or `share_denied_cap_reached` on a row
refused a URL, always accompanied by `denied_url=<url>` and
`denied_held_by=<course_id>` so the refusal can be judged without re-running
anything.

Evidence-quality flags: `hub_page_match` (a category page, never `verified`),
`name_from_slug` (sitemap-derived name), `name_from_page` (name read from the
target page, so Verification would agree with itself — never `verified`).

Other resolution flags: `url_claimed_by_stronger_match`,
`displaced_took_lower_candidate`, `shared_with_duplicate_row`,
`offsite_url_dropped=<host>`, `live_title_weaker_than_listing`,
`verify_http_<code>`, `catalog_candidates=<n>`.

`prior_note_disagrees` is worth watching: it marks rows a previous manual pass
recorded as `not found` where this pipeline did find a URL. That count is the
direct measure of how much the manual attempt missed.

## Crawl politeness

One request per second per domain, ~8 domains concurrently, `robots.txt`
honoured, a descriptive `User-Agent`, and every response cached so no URL is
fetched twice across runs. Some Sites are protected — CSU returns 403 to a plain
request — so the fetcher backs off rather than retrying hard.

## Measured results

**The figures below are from the previous sheet and are retained only as
regression canaries. They are not a prediction for `final_courses.csv`, which is
five times larger with a different Institution mix.** Re-measure before quoting
any coverage number — `coverage_report.md` is the only current source.

From a 12-Institution run on `processed_courses.csv` (3,921 rows, 2026-08-28):
38.8% filled; 17.3% `verified`; per-Institution fill from 79% (Charles Darwin)
to 0% (ACU, which answers 403 to every probe). Those four canaries —
Aberystwyth 71%, CDU 79%, Adelaide 65%, Curtin 58% — are what a change should
not regress.

### What sharing changed

On real Sydney rows (709) matched against their own prior URLs, the sharing rule
produced **76 Share Groups covering 163 rows**, every sampled one a base course
plus its `(Honours)` or duplicate variant — for example
`Bachelor of Languages` and `Bachelor of Languages (Honours)` on
`.../bachelor-of-languages.html`. Under the old rail one of each pair would have
been blanked.

### Prior URLs beat path probing, by a lot

The same 709 Sydney rows:

| Catalog source | rows filled |
|---|---|
| hub-path probing + crawl | 67 (9.4%) |
| the sheet's own prior URLs | 453 (63.9%) |

Path probing found `short-courses.sydney.edu.au` and spent the whole budget
there; the prior URLs point at `sydney.edu.au/courses/`. This is why prior URLs
are now crawl seeds as well as Candidates.

## Known limitations

**Discipline qualifiers in parentheses can be outweighed by a matching stem.**
The one error found in the sampled audit: ANU's `Master of Philosophy (Law)`
was matched to `science.anu.edu.au/study/research/master-philosophy-mphil` —
the right award, the wrong faculty. `Master of Philosophy` dominated the score
and `(Law)` did not veto it. Rows whose identity rests entirely on a
parenthesised discipline are the residual risk in `verified`.


**Extraction quality varies enormously by Institution, and this dominates
everything else.** The matcher is not the bottleneck; reaching the right
listing pages is. Aberystwyth publishes its whole catalogue as server-rendered
listings and fills ~73% of rows. Institutions behind JavaScript course finders
(Abertay) yield nothing at all and are reported `no_catalog`. A separate class
again simply **refuses** the crawler: ACU answers 403 to all 16 hub-path
probes, and no crawl tuning reaches it.

**Extraction Health is a proxy, not a measurement.** It compares Candidate
count against Course Row count, which detects "found too little" but not
"found the wrong things". Coventry once passed the count check with 217
Candidates while filling 4% of rows, because its Catalog had been assembled
from an unrelated sub-site's sitemap. `coverage_report.md` flags this shape as
`MISTARGETED` and lists it under **Fixable by crawling** — a crawling bug, not
a reason to buy a search API.

**Read the report's two failure sections as different problems.** *Fixable by
crawling* (`MISTARGETED`, `NO CANDIDATES`, `THIN`, `NO HUB`) means the Catalog
fell short and costs nothing but effort to improve. *Not fixable by crawling*
(`BLOCKED`, `NO WEBSITE`) means the site declines automated access; responding
to that is a policy decision, not a bug fix, and those rows must not be counted
as recoverable by crawling. The report also lists **seeds that yielded
nothing**, which is how a bad probe shows itself — the `catalogue` subdomain
probe collides with *library* catalogues at ACU, Abertay and Curtin, while
`handbook.` is genuinely productive.

**Prior-URL fills cannot reach `verified`, so coverage and review load rise
together.** A Candidate named from the page it points at cannot be corroborated
by Verification, which reads that same page — agreement would be circular. Those
matches are capped at `probable` and flagged `name_from_page`. On the Sydney run
this meant 446 rows filled but 445 in the Review Queue and only 1 `verified`.
That is the honest reading of the evidence available, not a defect; the way to
convert those into `verified` is better *listing* extraction, because a listing
name is independent of the target page.

**`matched_score` is not yet calibrated.** The sheet contains only four usable
ground-truth URLs, so the thresholds are reasoned rather than fitted. Until
`calibration_sample.csv` is labelled, read the score as a ranking, not a
probability. See [docs/CALIBRATION.md](./docs/CALIBRATION.md).

**A course that no longer exists cannot be found.** Some rows describe courses
the Institution has withdrawn — `Equine Science BSc (Hons)` is absent from
Aberystwyth's current catalogue, which lists only an MRes and a related
Bioscience BSc. `no_match` is the correct answer for these, and no amount of
extra spending changes that. The `prior_note_disagrees` count in the coverage
report helps separate stale rows from ones the earlier manual pass simply
missed.

**Ambiguity is often genuine.** Institutions list `X`, `X (Top-Up)`,
`X (with foundation year)` and `X (with integrated year in industry)` as
separate courses while the CSV names only `X`. These rows are marked
`ambiguous` and sent to review rather than guessed at.

## Testing

```bash
python -m unittest discover -s tests -v
```

The unit tests encode the real cases that justify the design: the two Data
Science variants must resolve to different URLs with a Margin above 0.3, and
`Equine Science BSc (Hons)` must score its BSc page above the MSc page. See
`docs/CALIBRATION.md` for how thresholds are fitted.
