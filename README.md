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
| what Phase 2 does and what it costs | [docs/PHASE-2.md](./docs/PHASE-2.md) |
| where a URL came from and what changed | [docs/PROVENANCE.md](./docs/PROVENANCE.md) |
| why we choose between two URLs with a gate | [docs/adr/0007](./docs/adr/0007-gated-prior-url-adoption.md) |
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
- **Results survive a kill.** Each Site's rows are written to `pipeline.db` as
  it completes, not at the end of the run, so an interrupted run keeps what it
  finished.
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

### Reading a run afterwards

Every stage logs, and `logger` names the stage — `pipeline.load`,
`pipeline.catalog`, `pipeline.match` — so a run is filterable without
re-executing it.

| level | per site | full run | what it holds |
|---|---|---|---|
| INFO (default) | ~5 records | **~13 MB** | `load.done`, `seeds.found`, `crawl.done`, `ladder.step`, `catalog.built`, `match.done` |
| DEBUG (`--verbose`) | ~870 records | **~92 MB** | plus `crawl.page`, `crawl.skip`, `match.row`, `verify.row` |

The INFO records *show* the run rather than describing it: `seeds.found`
carries the actual seed URLs, `catalog.built` a bounded sample of
`(name, url)` Candidates, `match.done` sample matches with their scores. Full
Catalogs live in `catalogs/<host>.json`; the log points at them rather than
copying thousands of Candidates into every run.

`crawl.skip` is aggregated, not per link — `crawl.done` counts every skipped
link by reason, while only a handful are named individually: those whose anchor
text *looks like a course* but whose path was rejected. That is the Coventry
failure, where 188 plausible course links were silently discarded by a path
filter. Naming every rejected link instead produced 40,562 records for a single
site.

Retention is `--keep-logs`, default 20 previous runs, `0` to keep everything.
Runs are pruned whole — JSONL, readable log and rotation backups together — and
the current run is never touched. Ordering is by file mtime rather than by name,
because the readable run id sorts *before* the older compact one.

### Long runs

`nohup … &` is not enough — a full run launched that way died with its parent
shell after 8 sites. Detach it properly:

```bash
setsid nohup python3 -u run.py --workers 16 < /dev/null >> logs/console.out 2>&1 &
```

Both forms of the log land in `logs/` on their own — `<run_id>.jsonl` for
querying and `<run_id>.log` for reading — so the redirect above only catches
anything printed before logging starts, such as a traceback during argument
parsing. `logs/` and any stray `*.log` are gitignored.

Interrupting is safe either way: the CSVs are written only at the end, so a
killed run produces no partial outputs, but every fetched page and extracted
Catalog is already on disk and the next run resumes from them.

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

## Running in chunks

A full run over `final_courses.csv` takes several hours. Chunking does **not**
make that total smaller — it delivers a complete vertical slice (Phase 1 →
Phase 2 → accuracy) on part of the sheet early, so problems surface before
Phase 2 spends money on all 52,781 rows.

```bash
python tools/split_by_site.py --chunks 20 --report   # plan only, no files
python tools/split_by_site.py --chunks 20            # writes chunks/

python run.py --input chunks/final_courses.001.csv   # one chunk
python run.py --input chunks/final_courses.002.csv
#   ...
python tools/merge_chunks.py                         # combine the results
```

Output paths are suffixed automatically from the chunk number
(`courses_filled.001.csv`), so chunks cannot overwrite each other. Pass
`--out` and friends explicitly to override.

### Chunks are cut on Site boundaries, never row ranges

This is the property the whole scheme depends on. Institutions are contiguous
in the sheet but **Sites are not** — 165 hosts are shared by 455 Institution
records, because `britishcouncil.org` appears fifteen times as separate
per-country entries. A row-range split fractures **142 Site buckets**, and a
fractured Site gets its Catalog built twice from two partial row sets, has the
Variant Sibling sharing rule applied to each half separately, and has its
Extraction Health measured against a row count that is missing rows.

`tools/split_by_site.py` therefore imports `load_rows` and `group_by_site`
rather than reimplementing the Site rule, so the split and the pipeline agree
by construction.

### Equal rows is not equal time

Chunks are balanced on row count, but wall-clock tracks **Site count**, because
most of the cost is per-Site probing. At 20 chunks expect roughly 1–27 minutes
each. One chunk is a single 5,083-row Site — the ANZSCO visa occupation codes
on `homeaffairs.gov.au`, which are not courses at all and which no crawl can
resolve. `--report` prints the spread before you commit to a split.

The 60 rows with no usable `website` go to `chunks/no_website.csv` rather than
into a chunk: they cannot be crawled, and padding them into a chunk would
inflate its row count while costing no time.

### Staleness

`chunks/manifest.json` records a SHA-256 of the sheet the chunks were cut from.
`run.py` warns when a chunk no longer matches the current `final_courses.csv`.
Regenerate chunks with the tool after the sheet changes — never edit a chunk by
hand.

## Phase 2: triage and search

Phase 1 finished the full sheet at **24,294 of 52,703 rows filled (46.1%)**,
though only 9.1% are `verified`. Phase 2 works over that result file without
crawling anything.

```bash
# Prior-URL triage only. No network, no vendor, no cost.
python run.py --phase 2 --results courses_filled.csv --out phase2.csv
```

**Triage** chooses between our URL and the one the source sheet already had,
using a quality gate rather than either pipeline's confidence label. Fill rises
**46.1% → 51.0%**. Full reasoning and the measurement behind it:
[docs/adr/0007](./docs/adr/0007-gated-prior-url-adoption.md).

**Search fallback** is built but **inert until a vendor is configured** — the
default provider returns nothing, so the stage reports zero results rather than
failing. About 19,000 rows are genuine search targets, roughly $19 at Serper
rates. See [docs/PHASE-2.md](./docs/PHASE-2.md).

### The outputs gain three columns

`prior_course_url`, `prior_matched_status` and `url_change` — because the
pipeline used to overwrite the sheet's URL and keep no record, silently altering
**38.4% of rows**. They appear in `courses_filled.csv`, `review_queue.csv` and
the Phase 2 output alike, so the sheet's URL is visible wherever a decision is
being read or made. What they mean and how to read them:
[docs/PROVENANCE.md](./docs/PROVENANCE.md).

`url_change` describes *that file's own* answer against the sheet, so a row may
read `dropped` in `courses_filled.csv` and `unchanged` after Phase 2 restored
it. Both are correct for the file they are in.

**This is a schema change.** The columns are *appended*, so anything reading by
column name is unaffected — but a consumer reading by column position will
break.

`matched_status` gains `carried_over`, for a row whose URL came from the sheet
rather than from extraction.

## Outputs

| File | Contents |
|---|---|
| `courses_filled.csv` | all input columns plus `course_url`, `matched_score`, `matched_status`, `match_margin`, `live_page_score`, `match_evidence`, `row_flags` |
| `review_queue.csv` | only rows needing human triage, worst Margin first, both competing Candidates shown side by side |
| `coverage_report.md` | coverage %, Review Queue size, per-Institution Extraction Health — the input to the Phase 2 spend decision |
| `calibration_sample.csv` | ~150 stratified rows for one-time human labelling to fit thresholds |
| `pipeline.db` | run state, per-row results and URL history (ADR-0005) |
| `logs/<run_id>.jsonl` | one JSON object per event, with `site_key`, `candidates`, `failure_reason` and friends as top-level fields |
| `logs/<run_id>.log` | the same run as readable console lines |

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

**A refusal outranks every other diagnosis.** A site whose own host rejects the
crawler is reported `blocked` even when a satellite subdomain works and even
when the Catalog merely ended up thin. This matters because the two live in
different sections of the report: `blocked` is *not fixable by crawling*, and
mistaking it for `thin` sends a reader hunting a crawler bug that does not
exist. Detection reads the statuses observed on the host **most rows actually
name** — measured per full host, since collapsing `www.newcastle.edu.au`
(403×60) with `internationalcollege.newcastle.edu.au` (200×37) hides the
refusal completely.

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
