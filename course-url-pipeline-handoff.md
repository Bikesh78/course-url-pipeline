# Handoff — course_url pipeline

**Repo:** `/home/savdhan/Documents/course_url`
**Date:** 2026-09-01
**State:** branch `feature/bounded-url-sharing`, 201 tests passing, uncommitted.
Previous state (`main`) is 152 tests on the old sheet.

## Read these first (do not re-derive)

| What | Where |
|---|---|
| Domain vocabulary | `CONTEXT.md` |
| Why extraction, not URL generation | `docs/adr/0001-extract-catalogs-never-generate-urls.md` |
| Why constrained Assignment (SUPERSEDED) | `docs/adr/0002-constrained-assignment-not-independent-matching.md` |
| When two courses may share a URL | `docs/adr/0004-bounded-url-sharing.md` |
| Why results live in SQLite too | `docs/adr/0005-sqlite-for-run-state-and-url-history.md` |
| Why crawling is per-site, not per-institution | `docs/adr/0006-the-crawl-unit-is-a-website-not-an-institution.md` |
| Why Phase 1 is free/stdlib | `docs/adr/0003-free-deterministic-phase-one.md` |
| Usage, outputs, measured results, known limitations | `README.md` |
| Threshold fitting procedure | `docs/CALIBRATION.md` |
| Last run's numbers + per-institution diagnosis | `coverage_report.md` (gitignored, on disk) |
| Original approved plan | `/home/savdhan/.claude/plans/to-create-pipeline-to-compressed-moonbeam.md` |
| Full prior conversation | `2026-08-28-172120-command-messagegrill-with-docscommand-message.txt` (committed in repo) |

The README's **Measured results** and **Known limitations** sections carry the
headline numbers and the one known error class. Don't restate them; check them.

## Git state

- Remote: `git@github.com:Bikesh78/course-url-pipeline.git`. Both branches pushed.
- `main` = pipeline commit `ce28216` (22 files, 3,780 lines) + `9585926`
  "Add session" (conversation transcript + this handoff file).
- `feature/course-url-pipeline` points at `ce28216` — fully contained in
  `main`, so it is now redundant and can be deleted.
- The 1,885-line conversation transcript is tracked in the repo as of
  `9585926`. Confirm with the user whether they want that tracked; it may have
  been incidental.

## What was decided (by the user, during a grilling session)

These are the constraints the code implements. Changing any of them is a
product decision, not a refactor:

1. **Fill the best candidate with a confidence score**, rather than
   verify-or-blank. Humans triage the tail via `review_queue.csv`.
2. ~~No two Course Rows may share a URL.~~ **Superseded 2026-09-01.** Course
   URLs are not unique: one page can hold several courses, and
   `Anthropology`/`Anthropology with Placement` are the same page. Sharing is
   now allowed between **Variant Siblings** only — matching Variant Stem and
   Award class — capped at 8, with refusals recorded. (ADR-0004.)
3. **Tiered hybrid** — per-institution Catalog extraction now; paid search
   fallback deferred to Phase 2.
4. **Phase 1 spends nothing**, so the coverage number is measured rather than
   estimated, and the spend decision is made against real data. (ADR-0003.)
5. **The input sheet is read-only.** The current sheet is
   `final_courses.csv` (52,781 rows, 2,548 Institutions); `processed_courses.csv`
   is a strict subset of it and is retained only for comparison runs. Its
   28,835 prior `course_url` values and 7,968 `program_link`s are crawl seeds
   and Candidate material, never authority.
6. Prior `Notes` annotations are evidence, never authority — 222 `not found`
   entries are known to contain false negatives.
7. Polite crawling: 1 req/s per domain, ~8 domains concurrent, robots.txt
   honoured.
8. **The crawl unit is a website host, not an Institution** — 165 hosts are
   shared by 455 Institutions covering 24.5% of rows. (ADR-0006.)
9. **Runs are traceable**: `run_id`-stamped JSONL logs in `logs/`, run state and
   per-Course-Row URL history in `pipeline.db`. (ADR-0005.)

## Current status of the work

**Done and verified on this branch (201 tests, all passing):**

- Bounded URL sharing (ADR-0004) with the Variant Stem test, the cap, and
  recorded refusals.
- Candidates keyed on `(url, variant_stem)`, so one page can hold several
  courses.
- Migration to `final_courses.csv`, keyed on `institution_id`.
- Site-host bucketing (ADR-0006), reducing 2,548 Institutions to 2,220 crawl
  units.
- `registrable()` fixed: it was collapsing `barkly.vic.edu.au` to `vic.edu.au`,
  putting 144 unrelated Victorian schools in one bucket. Harmless as a
  rate-limit key, wrong as a Catalog key.
- Prior URLs used as crawl seeds *and* Candidates, named from the target page.
- `pipeline/store.py` (SQLite run state + URL history) and
  `pipeline/logging_setup.py` (JSONL per-run logs).
- 5,014 ANZSCO visa occupation rows flagged `occupation_code_not_course`.

**Measured on one real site (Sydney, 709 rows):** fill went from **67 (9.4%)**
with path probing alone to **446 (62.9%)** once prior URLs seeded the crawl.
74 Share Groups formed, **none with mismatched stems**, largest group 4.

**Not done:**

- **No full run on the new sheet.** Only Sydney has been run end to end. Expect
  8–12 hours for all 2,220 Sites.
- **The deferred extraction work is still deferred** — host promotion mid-crawl
  and language-mirror demotion (plan section 7). Prior-URL seeding addressed the
  same symptom more directly, so re-measure before building them.
- **`calibration_sample.csv` is unlabelled**, so `matched_score` remains a
  ranking rather than a probability.
- **The Phase 2 spend decision is still open.**

## Highest-value next work, in order

1. **Run the full sheet** and read `coverage_report.md`. Every design decision
   above is now evidence-backed on one site; none is evidence-backed at scale.
2. **Look at the review-queue size before anything else.** Prior-URL fills are
   capped at `probable` because their name comes from the page Verification
   would re-read, so coverage and review load rise together — Sydney filled 446
   and queued 445. If that ratio holds at scale, improving *listing* extraction
   (which corroborates independently) matters more than raising coverage again.
3. **Label the calibration sample**, then record fitted thresholds in
   `docs/CALIBRATION.md`.
4. **Then** decide on Phase 2.

## Gotchas that are not obvious from the code

- **After changing anything in `pipeline/catalog.py`, pass
  `--refresh-catalogs`.** Otherwise stale `catalogs/*.json` are reused and your
  change appears to do nothing. `SCHEMA_VERSION` in `catalog.py` only forces a
  rebuild when the JSON *shape* changes, not when extraction logic changes.
- **`.cache/` is 585MB+ and gitignored.** The full run will be several GB and
  several hundred thousand files. New entries are gzipped; pre-existing
  uncompressed entries are still read, so do not delete the cache to "clean
  up" — it is what makes reruns free. `pipeline.db` deliberately does *not*
  store pages (ADR-0005).
- **Never reorder verification work by Institution.** `run.py`'s
  `interleave_by_domain()` exists because grouping by Institution parks every
  worker on one domain lock and costs a 12× slowdown.
- **Two guards are load-bearing and must not be relaxed to raise coverage:**
  the Award/Level guard and the uniqueness rail. `docs/CALIBRATION.md` explains
  why. Coverage bought by weakening either is silent error.
- **A negative `match_margin` is meaningful**, not a bug: the row was displaced
  and holds a second choice.
- **Never name a prior URL after the Course Row that claimed it.** The earlier
  pipeline assigned one Sydney page to five rows spanning three courses; naming
  Candidates after those rows handed a Veterinary Biology row a page belonging
  to Animal and Veterinary Bioscience. `harvest_prior_urls` reads the page's own
  heading for exactly this reason.
- **`--limit N` ranks by plausible-course rows, not raw rows.** The biggest
  bucket in the sheet is 5,083 visa occupation codes.
- **Some rows are genuinely unresolvable.** Courses get withdrawn;
  `Equine Science BSc (Hons)` no longer exists at Aberystwyth. `no_match` is
  the correct answer and no spending changes it.

## Suggested skills

- **`superpowers:verification-before-completion`** — mandatory here. Several
  defects in this build were only caught by checking real output against the
  claim, including a repair heuristic that silently corrupted 38 good rows and
  a "healthy" Catalog that filled 4% of its Institution.
- **`diagnosing-bugs`** or **`superpowers:systematic-debugging`** — for the
  extraction suspects (ANU, Concordia, ACU). The productive method last time
  was to fetch the hub page and count how many plausible course links survive
  each filter, rather than reasoning about the code.
- **`superpowers:test-driven-development`** — every extraction change should
  start as a failing test using a real observed URL/title. The existing tests
  in `tests/test_normalize.py` and `tests/test_catalog.py` follow that pattern.
- **`domain-modeling`** — if new vocabulary appears (e.g. a Phase 2 "Search
  Result" concept), update `CONTEXT.md` inline and add an ADR only if the
  decision is hard to reverse.
- **`superpowers:brainstorming`** — before building any Phase 2 capability.
- **`grilling`** — only if reopening one of the seven decisions above.

## Do not

- Write to `processed_courses.csv`.
- Generate URLs from name patterns (ADR-0001 records the measurement showing
  this produces live, plausible, wrong pages).
- Weaken the floor, the Award/Level guard or the Variant Stem test to raise
  coverage. Those three are what prevent the 116-way collapse and the
  Anthropology/Archaeology confusion; ADR-0004 records the measurements.
- Claim a coverage figure without regenerating `coverage_report.md` first.
