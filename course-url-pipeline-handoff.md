# Handoff — course_url pipeline

**Repo:** `/home/savdhan/Documents/course_url`
**Date:** 2026-08-28
**State:** committed and pushed, working tree clean, 112 tests passing.

## Read these first (do not re-derive)

| What | Where |
|---|---|
| Domain vocabulary | `CONTEXT.md` |
| Why extraction, not URL generation | `docs/adr/0001-extract-catalogs-never-generate-urls.md` |
| Why constrained Assignment | `docs/adr/0002-constrained-assignment-not-independent-matching.md` |
| Why Phase 1 is free/stdlib | `docs/adr/0003-free-deterministic-phase-one.md` |
| Usage, outputs, measured results, known limitations | `README.md` |
| Threshold fitting procedure | `docs/CALIBRATION.md` |
| Last run's numbers + per-institution diagnosis | `coverage_report.md` (gitignored, on disk) |
| Original approved plan | `/home/savdhan/.claude/plans/to-create-pipeline-to-compressed-moonbeam.md` |
| Full prior conversation | `2026-08-28-172120-command-messagegrill-with-docscommand-message.txt` (committed in repo) |

The README's **Measured results** and **Known limitations** sections carry the
headline numbers and the one known error class. Don't restate them; check them.

## Git state

- `main` = pipeline commit `ce28216` + transcript commit `0451aeb`. Pushed.
- `feature/course-url-pipeline` points at `ce28216` — fully contained in `main`,
  so it is now redundant and can be deleted.
- The 107KB conversation transcript was committed to the repo in `0451aeb`.
  Confirm with the user whether they want that tracked; it may have been
  incidental.

## What was decided (by the user, during a grilling session)

These are the constraints the code implements. Changing any of them is a
product decision, not a refactor:

1. **Fill the best candidate with a confidence score**, rather than
   verify-or-blank. Humans triage the tail via `review_queue.csv`.
2. `course_url` is semantically a payload attached to `id`, but the user
   explicitly opted into join-grade safety: **no two Course Rows in one
   Institution may share a URL unless flagged.** (ADR-0002.)
3. **Tiered hybrid** — per-institution Catalog extraction now; paid search
   fallback deferred to Phase 2.
4. **Phase 1 spends nothing**, so the coverage number is measured rather than
   estimated, and the spend decision is made against real data. (ADR-0003.)
5. **`processed_courses.csv` is read-only.** Verified byte-identical.
6. Prior `Notes` annotations are evidence, never authority — 222 `not found`
   entries are known to contain false negatives.
7. Polite crawling: 1 req/s per domain, ~8 domains concurrent, robots.txt
   honoured.

## Current status of the work

**Done and verified:** the whole Phase 1 pipeline, 112 tests, golden case
(Aberystwyth) and adversarial case (ACU → `no_catalog`, zero rows filled)
both behaving correctly, all output invariants passing.

**Not done:**

- **The full run has never been executed.** Only the 12 largest Institutions
  (3,921 of 10,637 rows) have been processed. `python run.py` does all 555.
- **`calibration_sample.csv` (135 rows) is unlabelled.** Until it is,
  `matched_score` is an ordering signal, not a probability. Procedure is in
  `docs/CALIBRATION.md`.
- **The Phase 2 spend decision is open** and is the point of the whole
  exercise. `coverage_report.md` is its input.

## Highest-value next work, in order

1. **Fix the extraction suspects before spending any money.** ANU (409 rows,
   9% filled) and Concordia (226 rows, 7%) both have large Catalogs but almost
   no fills, which means the crawler reached the wrong pages. ACU extracts
   literally zero Candidates. That is ~835 rows recoverable by crawling, not
   purchasing. `coverage_report.md` flags this shape as `EXTRACTION SUSPECT`.
2. **Run the full 555 Institutions** and read the resulting coverage report.
   Expect several hours; it is resumable and reruns are nearly free.
3. **Label the calibration sample**, then record fitted thresholds in
   `docs/CALIBRATION.md`.
4. **Then** decide on Phase 2 (paid search / LLM adjudication / headless
   rendering). All three are designed as plug-ins behind the Catalog ladder
   and the Assignment adjudication hook.

## Gotchas that are not obvious from the code

- **After changing anything in `pipeline/catalog.py`, pass
  `--refresh-catalogs`.** Otherwise stale `catalogs/*.json` are reused and your
  change appears to do nothing. `SCHEMA_VERSION` in `catalog.py` only forces a
  rebuild when the JSON *shape* changes, not when extraction logic changes.
- **`.cache/` is 585MB for 12 Institutions** and gitignored. The full run will
  be several GB. New entries are gzipped; pre-existing uncompressed entries are
  still read, so do not delete the cache to "clean up" — it is what makes
  reruns free.
- **Never reorder verification work by Institution.** `run.py`'s
  `interleave_by_domain()` exists because grouping by Institution parks every
  worker on one domain lock and costs a 12× slowdown.
- **Two guards are load-bearing and must not be relaxed to raise coverage:**
  the Award/Level guard and the uniqueness rail. `docs/CALIBRATION.md` explains
  why. Coverage bought by weakening either is silent error.
- **A negative `match_margin` is meaningful**, not a bug: the row was displaced
  and holds a second choice. 618 rows in the last run were left blank by the
  uniqueness rail — those are variants the Institution does not publish as
  separate pages, so a search API will not resolve them either.
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
- Claim a coverage figure without regenerating `coverage_report.md` first.
