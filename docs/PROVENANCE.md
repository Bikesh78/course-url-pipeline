# Where a URL came from, and what happened to it

Two questions get asked about every row, and they are **not the same question**:

- *Did the answer change?* → `url_change`
- *Where did the answer come from?* → `matched_status`

They live in separate columns on purpose. Collapsing them into one is what makes
provenance columns become ambiguous six months later.

## Why these columns exist

The pipeline used to overwrite the sheet's `course_url` with its own and keep no
record. Measured across the full sheet, that silently altered **38.4% of rows**:

| what happened to the sheet's URL | rows | share |
|---|---|---|
| neither side had one | 17,576 | 33.3% |
| **the prior URL was dropped** | 8,241 | 15.6% |
| **the URL was changed** | 8,328 | 15.8% |
| unchanged | 12,265 | 23.3% |
| a URL was added | 6,293 | 11.9% |

334 of the changed rows had replaced a working URL with one that returned 404,
and nothing in the output said so.

## `url_change`

| value | meaning |
|---|---|
| `none` | the sheet had no URL, and neither do we |
| `added` | the sheet had none; we found one |
| `unchanged` | same page as the sheet |
| `changed` | both have a URL, and they are different pages |
| `dropped` | the sheet had one; we deliver none |

**A trailing slash, a URL fragment, and tracking query parameters are not
changes.** `/courses/x` and `/courses/x/` are one page. Before that was fixed,
729 rows reported a change that was only punctuation.

`url_change` is computed **after** triage, so a row whose prior URL is restored
reads `unchanged` rather than `dropped`. The column describes what you actually
received, not an intermediate state the pipeline passed through.

## Which files carry these columns

**Both.** `courses_filled.csv` (Phase 1) and the Phase 2 output each carry all
three, and `review_queue.csv` carries them too — a reviewer choosing between our
match and its runner-up should see the sheet's URL as well, since on `ambiguous`
rows a `matched` prior beat our result 1,613 times to 498.

`url_change` describes **that file's own answer** against the sheet. Phase 2
recomputes it after triage, so a row can legitimately read `dropped` in
`courses_filled.csv` and `unchanged` in the Phase 2 output — Phase 1 found
nothing, and triage then restored the sheet's URL. Each file is telling the
truth about what it delivered; neither is stale.

## The companion columns

- `prior_course_url` — the sheet's URL, verbatim, never overwritten
- `prior_matched_status` — the sheet's own label (`matched`, `low_confidence`,
  `unmatched`), so you can weigh it yourself
- `matched_status` — ours, including `carried_over` when the delivered URL came
  from the sheet rather than from extraction

Reading them together:

| `url_change` | `matched_status` | what it tells you |
|---|---|---|
| `unchanged` | `carried_over` | we had nothing or something worse; the sheet's URL was kept |
| `unchanged` | `verified` | we independently found the same page — the strongest signal available |
| `changed` | `verified` | we replaced the sheet's URL and confirmed ours against the live page |
| `changed` | `ambiguous` | we replaced it with something we are unsure about — worth reviewing |
| `dropped` | `no_catalog` | the site could not be read at all; the sheet's URL failed our quality gate |

## Asking what has moved over time

`pipeline.db` keeps a dated history per course. The sheet's own URLs are seeded
as each course's first entry, stamped with the sheet's `processed_date`
(2026-06-24, 06-25 or 07-14) rather than an invented timestamp — so the history
is honest about when a URL was actually established.

```sql
-- courses whose URL has moved since the sheet was produced
SELECT course_id, COUNT(*) AS urls
FROM url_history GROUP BY course_id HAVING urls > 1;

-- the full history of one course
SELECT url, first_seen, last_seen, last_verified, status
FROM url_history WHERE course_id = ? ORDER BY first_seen;
```

As of the first Phase 2 run, **8,328 courses** hold a different URL from the one
the sheet delivered — which is exactly the `url_change = changed` count in the
CSV. The two are computed independently, so their agreement is a check rather
than a coincidence.
