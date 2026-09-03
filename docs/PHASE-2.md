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
gave the Endodontics page to Orthodontics as well. 1,949 adoptions were refused
for this reason. Without that check, Phase 2 would import the exact failure
[ADR-0004](./adr/0004-bounded-url-sharing.md) exists to prevent.

Result: **46.1% → 51.0% filled**, at no cost and with no network.

## Stage 2 — Search fallback (needs a vendor)

After triage, **24,256 rows** still have no URL. Two thirds of them are
`no_catalog`: the crawler could not read the site at all, so no amount of crawl
tuning will reach them. A search engine already has.

The rule that keeps this safe: **a search hit is a candidate, not an answer.**
It is scored against the course name, required to be on the institution's own
domain, put through the same sharing rule as a crawled URL, and can never be
reported `verified` on the strength of a search ranking. "It was the top result"
is evidence about the search engine, not about the course.

Rows deliberately not searched: the **5,008** flagged
`occupation_code_not_course` are ANZSCO skilled-migration occupation codes
("… - 411511 (subclass 186)"), not courses. No page exists to find, and querying
for them would spend money on a guaranteed miss.

That leaves roughly **19,000–20,000 genuine targets**, which is about **$19** at
Serper's rate, or free within Brave's monthly allowance for the first few
thousand.

### "No vendor configured" — what that means today

No search provider has been chosen, so `NullProvider` is the default and returns
nothing. Phase 2 runs end to end and reports `0 on-site results`; it does not
fail. The wiring is proven by `FixtureProvider`, which serves canned results
from a JSON file with no key and no network.

**So today, all of Phase 2's measurable gain comes from Stage 1.** Stage 2 is
built and tested but inert until someone provisions a key.

## Running it

```bash
# Triage only (no vendor needed)
python run.py --phase 2 --results courses_filled.csv --out phase2.csv

# With canned search results, to exercise the whole path offline
python run.py --phase 2 --results courses_filled.csv \
    --search-fixture fixtures/search.json --out phase2.csv
```

## Reading the output

Phase 2 appends three columns — `prior_course_url`, `prior_matched_status` and
`url_change`. What they mean, and how to read them alongside `matched_status`,
is in [PROVENANCE.md](./PROVENANCE.md).

`matched_status` gains one new value:

| status | meaning |
|---|---|
| `carried_over` | the delivered URL came from the source sheet, not from extraction |

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
