# Phase 2: choosing between two answers, and asking a search engine for a third

Phase 1 crawls each institution's own site and matches courses against what it
finds. It finished the full sheet with **24,294 of 52,703 rows filled (46.1%)** —
but only 9.1% of those are `verified`. Phase 2 is what happens next, and it has
two stages that are independent of each other.

Neither stage crawls anything. Phase 2 reads an existing Phase 1 result file.

## Stage 1 — Prior-URL triage (free, no network, no vendor)

The input sheet arrived with a previous pipeline's `course_url` already filled
on 28,835 rows. So for many courses there are now two candidate answers: ours
and the sheet's. Triage picks the better one.

It does **not** trust either pipeline's confidence label. It scores each URL's
own slug against the course name — a referee that has no idea which pipeline
produced what — and requires our existing floor of 0.55.

A prior URL is adopted in exactly three situations:

1. **We have nothing** and the sheet's URL clears the gate.
2. **Our URL is dead** (returned an error when verified) and the sheet's clears
   the gate.
3. **Our result is `ambiguous`**, the sheet's label was `matched`, and the
   sheet's URL scores higher.

Our `verified` results are never given up. Neither are our `probable` ones —
measured across 831 such disagreements, swapping was a coin flip (334 to 311),
so swapping would lose as often as it won.

Adopted rows are marked `matched_status = carried_over` and flagged
`url_from_source_sheet`, so a carried URL is never mistaken for one we found.

**Adoption still has to pass the sharing rule.** The sheet shares URLs across
different courses at 60% — it filed one page under 116 unrelated courses, and
gave the Endodontics page to Orthodontics as well. 1,974 adoptions were refused
for this reason. Without that check, Phase 2 would import the exact failure
[ADR-0004](./adr/0004-bounded-url-sharing.md) exists to prevent.

Result: **46.1% → 51.1% filled** (3,974 adoptions), at no cost and with no network.

## Stage 2 — Search fallback (needs a vendor)

After triage, **25,767 rows** still have no URL. Just under three fifths of them
(15,247) are `no_catalog`: the crawler could not read the site at all, so no amount of crawl
tuning will reach them. A search engine already has.

The rule that keeps this safe: **a search hit is a candidate, not an answer.**
`search_rows` puts every hit through four checks before it writes anything —
on the institution's own domain, scored against the course name, over the same
0.55 floor extraction answers to, and past the same sharing rule as a crawled
URL. It can never be reported `verified` on the strength of a ranking.

"It was the top result" is evidence about the search engine, not about the
course, so **the best-scoring hit wins, not the first one returned.** The
provider's order is a tiebreak and nothing more.

Rows deliberately not searched: the **5,008** flagged
`occupation_code_not_course` are ANZSCO skilled-migration occupation codes
("… - 411511 (subclass 186)"), not courses. No page exists to find, and querying
for them would spend money on a guaranteed miss.

That leaves **20,706 genuine targets** (the count the run reports), about **$21** at
Serper's rate, or free within Brave's monthly allowance for the first few
thousand.

### The provider

`SerperProvider` is the vendor implementation. It is **opt-in by name** — the
default is still `NullProvider`, so no run can spend money by accident:

```bash
cp .env.example .env                 # then fill in SERPER_API_KEY
python run.py --phase 2 --results phase2.csv --out phase2.searched.csv \
    --search-provider serper --search-limit 500
```

The key is read from the environment only — from a git-ignored `.env`, or from
a variable you set yourself. It is deliberately **not** a CLI option, because
`run.py` writes `vars(args)` into the `runs` table as JSON and a flag would
persist the credential in `pipeline.db`. There is a test asserting the key never
appears in the parsed arguments.

`.env` is loaded at startup by `pipeline/config.py` — stdlib, no dependency, and
no `${VAR}` interpolation, so the effective value of a credential is always what
you can read on the line. **An already-set variable wins**, so a one-off
`SERPER_API_KEY=... python run.py ...` and CI secrets both override the file.
`--env-file` points at a different one; that flag carries a path, not a secret.

Three things it does beyond calling the API:

- **Caches every response**, hits and misses alike, gzipped under
  `.cache/serper/` the way pages are cached. Phase 2 gets re-run often, and a
  re-run that re-asked 20,706 queries would cost as much as the first. An empty
  result is a fact about a query worth remembering.
- **Refuses to fail quietly.** A rejected key raises instead of returning
  nothing, because 20,706 rows of "no results" is indistinguishable in the log
  from a search engine that genuinely found nothing. `429`/`5xx` are retried
  twice with backoff; ten consecutive failures stop the run rather than spend
  further. A failure is never cached, so a retry later is still possible.
- **Can be capped.** `--search-limit N` bounds paid calls, so a live vendor can
  be tried on a few hundred rows before committing to the sheet. The run log
  reports calls made, cache hits and an estimated spend.

If the vendor breaks mid-run, the stage stops and says so, but the rows it
already filled stay filled and the output is still written — a broken Stage 2
is no reason to discard Stage 1's work.

### Running without a vendor

`NullProvider` returns nothing, so Phase 2 runs end to end, reports `0 adopted`,
and leaves every row exactly as it found it. Inert means untouched, not
half-written — verified by a regression check that the output is byte-identical
to a triage-only run.

The whole adoption path is exercised offline by `FixtureProvider` against
`fixtures/search.json`, which carries real unfilled rows from the sheet and
covers each rejection branch as well as the happy one: an aggregator URL, an
on-domain page that misses the floor, and one page two non-sibling courses both
want.

**Until a key is provisioned, all of Phase 2's measurable gain still comes from
Stage 1** — not because Stage 2 is unfinished, but because it has nothing to
ask.

## Running it

```bash
# Triage only (no vendor needed, nothing spent)
python run.py --phase 2 --results courses_filled.csv --out phase2.csv

# With canned search results, to exercise the whole adoption path offline
python run.py --phase 2 --results courses_filled.csv \
    --search-fixture fixtures/search.json --out phase2.csv

# Live, capped to 500 paid calls (key from .env)
python run.py --phase 2 --results phase2.csv --out phase2.searched.csv \
    --search-provider serper --search-limit 500
```

| flag | effect |
|---|---|
| `--search-provider` | `null` (default) or `serper`. Nothing spends unless named. |
| `--search-limit` | cap on paid calls this run |
| `--search-delay` | seconds between paid calls (default 0.2) |
| `--search-fixture` | canned results; wins over `--search-provider` |
| `--env-file` | where to read `KEY=value` lines from (default `.env`) |

Exit codes: `0` normal, `2` the provider could not be built (no key), `3` the
search stage aborted on the vendor mid-run. A `3` still writes the output file
— triage's work is valid and is not discarded because a vendor broke — so treat
it as "check the log", not "the run produced nothing".

## Reading the output

Phase 2 appends three columns — `prior_course_url`, `prior_matched_status` and
`url_change`. What they mean, and how to read them alongside `matched_status`,
is in [PROVENANCE.md](./PROVENANCE.md).

`matched_status` gains two new values:

| status | meaning |
|---|---|
| `carried_over` | the delivered URL came from the source sheet, not from extraction |
| `search_found` | the delivered URL came from a search result, scored and sharing-checked but never fetched |

Rows carry `url_from_search` alongside `search_found`, and a hit refused by the
sharing rule leaves the row unfilled and flagged `search_denied_sharing` — the
search counterpart of triage's `adoption_denied_sharing`.

## A worked example

`Diploma of Community Services` at ACI College.

- **The sheet said** `…/acic-course/chc52021-diploma-of-community-services/`,
  labelled `matched`.
- **Phase 1 found** `…/acic-course/chc52025-diploma-of-community-services/` and
  marked it `verified` — a newer qualification code, found on the live site.
- **Triage kept ours.** `verified` is never given up, and the row is written
  `url_change = changed`, so a reviewer can see the sheet's value and disagree
  if they want to.

And the opposite case, `Diploma of Agriculture` at ACAH:

- **The sheet said** `…/diploma-of-agriculture/`, labelled `matched`.
- **Phase 1 found** `…/all-courses/queensland-vocational-education…`, an
  `ambiguous` match at 0.775 — a category page, not the course.
- **Triage adopted the sheet's URL**, because our result was `ambiguous`, the
  prior was `matched`, and the prior scored higher. The row becomes
  `carried_over` with `url_change = unchanged`.
