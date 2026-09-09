---
status: accepted
---

# One result file, updated in place

Phase 1 wrote `courses_filled.csv` and phase 2 wrote `phase2.csv`. Same
columns, same 52,703 rows, same shape — so both looked like the answer and
nothing in either said which to open. Reported as confusing twice.

Adding a `phase` column (ADR pending in `docs/PROVENANCE.md`) did not help,
because the ambiguity was never inside a row. It was between files.

## Decision

**Phase 2 rewrites the file it reads.** `--out` defaults to `--results`, so
there is one result file for the whole pipeline. `--out` remains accepted, and
the search-batch and verification workflows use it.

The two-file split was carrying two real guarantees. Each is replaced rather
than abandoned:

### Phase 1's answer, replaced by two columns

`prior_course_url` holds the *sheet's* URL, not ours, so overwriting the file
used to destroy extraction's own answer for every row triage replaced — 1,307
genuinely different pages, recoverable only by a re-crawl.

`phase1_course_url` and `phase1_matched_status` now keep it in the row. Phase 2
back-fills them on reading a file written before they existed, where the
delivered URL *is* extraction's answer because nothing has touched it yet.

The one exception is a file that already holds phase-2 decisions: there the
delivered URL is not extraction's, and the file never recorded what was. Those
rows get blank phase-1 columns rather than a guess, because claiming a
carried-over URL came from extraction is a lie the rest of the pipeline would
then trust.

### Re-derivability, replaced by actual idempotency

Phase 2 used to be safe to re-run only because it always read a *different*
file. Re-running it over its own output adopted 9 more rows and refused 9 fewer
by the sharing rule — exactly offsetting, which pinned the cause to the share
index rather than to the gate.

Triage now decides from `phase1_course_url`, and `ShareIndex` seeds from it, so
"our answer" is what extraction found rather than what the last run delivered.
Phase 2 is now **idempotent**: verified byte-identical across three consecutive
runs, where before each run differed.

Two further leaks had to be closed to get there, neither visible in the
decision counts:

- **`search_found` had to stop being tradeable.** Triage runs before search and
  had never seen a search result. Reading the phase-1 columns, it would find
  extraction's answer empty, conclude "we have nothing", and adopt a prior URL
  over a search hit that cost money. 6 of the 131 rows in the first live trial
  had a gate-clearing prior.
- **Flags were appended unconditionally.** A re-run reached an identical verdict
  for every row and still produced a different file, because a refusal flag was
  recorded twice. Comparing flags as a set hid it. `add_flag` now appends only
  what is absent.

### The write is atomic

Writing the file just read means a crash mid-write would truncate the only
result there is. `write_rows_atomically` writes beside the target and
`os.replace`s it into position, keeping the previous version as `.bak` — the
same pattern `tools/backfill_provenance.py` uses, for the same reason.

## Consequences

**This reverses `5fbcf9a`**, which stopped `--phase 2` defaulting onto its own
input. That commit was right at the time: overwriting then destroyed the
baseline irrecoverably. It is recorded here as a deliberate reversal so it does
not later read as drift — the hazard it guarded against is gone, and only the
truncation risk remains, which the atomic write covers. The test asserting
`out != results` was replaced by tests for in-place writing, an explicit `--out`
still winning, and the baseline surviving in its column.

**The first run is unchanged.** On a file phase 1 wrote, `phase1_course_url`
and `course_url` are equal, so every decision is identical: verified
byte-identical to the previous `phase2.csv` once the three new columns are
stripped — 3,974 `carried_over`, 6,807 gate rejections, 1,974 sharing refusals.
Only re-runs changed, from drifting to stable.

**A file predating the phase-1 columns cannot be re-triaged identically.** Its
phase-2 rows have no recorded extraction answer, so triage re-derives them from
blank. That is a one-time migration artefact, not a property of the new flow:
any file phase 2 has written carries the columns and re-runs cleanly.

**`out/phase2.csv` is no longer produced.** The result is `out/courses_filled.csv`,
which phase 1 creates and phase 2 updates.
