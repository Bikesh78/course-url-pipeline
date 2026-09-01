# Course URL Pipeline

Fills the `course_url` column of `processed_courses.csv` (10,637 Course Rows
across 559 Institutions) by extracting each Institution's own course listings
and matching Course Rows against them, with a Score and Margin on every result.

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

## Guarantees

- **`processed_courses.csv` is never written to.** It is input only.
- **URLs are never constructed**, only selected from Institution listings
  (ADR-0001).
- **No URL is assigned to two Course Rows** within an Institution unless
  explicitly flagged `duplicate_url_collision` (ADR-0002).
- **No paid API, no LLM, no `pip install`** — stdlib plus the already-present
  `bs4`/`lxml` (ADR-0003).
- **Every stage is resumable.** All HTTP responses are cached under `.cache/`,
  and extracted Catalogs under `catalogs/`. An interrupted run resumes free.

## Requirements

Python 3.10+. `bs4` and `lxml` are used where present and degraded to stdlib
`re` parsing where not.

## Usage

```bash
# Full run over all 555 Institutions. Resumable: stop it and rerun freely.
python run.py

# Golden harness: one Institution with a known-good server-rendered Catalog
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
`--workers` (concurrent Institutions, default 8), `--input`, `--out`,
`--review-out`, `--report-out`, `--calibration-out`.

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
`year_level_not_course`, `truncated_name`, `malformed_row_repaired`,
`aggregator_website`, `unusable_website`.

Resolution flags: `url_claimed_by_stronger_match` (blank because a stronger row
took its best URL), `displaced_took_lower_candidate`, `hub_page_match`,
`name_from_slug`, `shared_with_duplicate_row`, `offsite_url_dropped=<host>`,
`live_title_weaker_than_listing`, `verify_http_<code>`,
`catalog_candidates=<n>`.

`prior_note_disagrees` is worth watching: it marks rows a previous manual pass
recorded as `not found` where this pipeline did find a URL. That count is the
direct measure of how much the manual attempt missed.

## Crawl politeness

One request per second per domain, ~8 domains concurrently, `robots.txt`
honoured, a descriptive `User-Agent`, and every response cached so no URL is
fetched twice across runs. Some Sites are protected — CSU returns 403 to a plain
request — so the fetcher backs off rather than retrying hard.

## Measured results

From a real run over the 12 largest Institutions (3,921 Course Rows, 2026-08-28,
default thresholds). This is evidence, not a projection.

| status | rows | share |
|---|---|---|
| `verified` | 678 | 17.3% |
| `probable` | 253 | 6.5% |
| `ambiguous` | 589 | 15.0% |
| `no_match` | 2,197 | 56.0% |
| `no_catalog` | 201 | 5.1% |
| `url_dead` | 3 | 0.1% |
| **filled** | **1,523** | **38.8%** |

Per-Institution fill rate varies by more than an order of magnitude, and
extraction — not matching — is what separates them:

| institution | rows | candidates | filled |
|---|---|---|---|
| Charles Darwin University | 220 | 598 | 79% |
| Aberystwyth University | 456 | 809 | 71% |
| Adelaide University | 534 | 1,292 | 65% |
| Curtin University | 390 | 867 | 58% |
| Cardiff Metropolitan University | 208 | 471 | 35% |
| Brunel University London | 395 | 390 | 29% |
| Bond University | 193 | 423 | 27% |
| Coventry University | 465 | 455 | 25% |
| Abertay University | 224 | 226 | 21% |
| Australian National University | 409 | 717 | 9% |
| Concordia University | 226 | 750 | 7% |
| Australian Catholic University | 201 | 0 | 0% |

Two invariant checks pass on that output: **no URL is assigned to two Course
Rows** (46 URLs are shared, every one of them by rows flagged
`shared_with_duplicate_row`), and **no filled row points off its Institution's
own domain**.

An eyeball audit of 18 randomly sampled `verified` rows across all 12
Institutions found 17 correct. Precision on `verified` is therefore high but
not perfect, and the observed error class is described below.

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
