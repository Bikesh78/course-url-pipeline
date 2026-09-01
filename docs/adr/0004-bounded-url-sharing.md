---
status: accepted
supersedes: ADR-0002
---

# Course Rows may share a URL, but only as Variant Siblings

ADR-0002 enforced one URL per Course Row within an Institution. That premise is
wrong in two ways. A single Institution page can hold several courses, and two
catalogue entries can legitimately be the same page — `Anthropology` and
`Anthropology with Placement` at many UK institutions, or a degree and its
`(Honours)` variant. The strict rail left 619 rows blank in a 12-site run for
no better reason than that a sibling had claimed their answer first.

Sharing is now permitted when the sharing rows are **Variant Siblings** — equal
Variant Stem and agreeing Award class — and capped at `SHARE_CAP` per URL. A row
whose best URL is held by a non-sibling stays blank with the denial recorded.

## Why the guard is a stem test and not a threshold

Score cannot make this distinction, and this is the measurement that decided the
design:

```
Anthropology BA  vs  Anthropology with Placement BA   = 0.775   must share
Anthropology BA  vs  Archaeology and Anthropology BA  = 0.775   must not
```

Identical scores, opposite correct answers. No floor and no Margin separates
them. `normalize.variant_stem()` does: `anthropology` in both sibling cases,
`archaeology anthropology` in the third.

## What was *not* protecting us

It is tempting to read the old rail as the defence against catastrophic
collapse. It was not. The source sheet contains 116 courses collapsed onto one
`master-of-laws-llm-with-placement-year` page, including "BA (Hons) 2D Digital
Animation"; the earlier pipeline matched on the shared words "with Placement".
This scorer rates those victims **0.130–0.141** against a floor of 0.55, because
it strips Awards and compares subject stems. The floor, the Award/Level guard
and subject-only comparison are what prevent collapse. Removing the rail
therefore costs far less than it appears — which is why relaxing it was safe and
why weakening *those three* would not be.

## Consequences

**Sharing keeps a meaning.** Because a denial leaves the row blank rather than
handing it the URL anyway, "these rows share a URL" always means "one course,
several variants". Had denials been filled anyway, a Share Group would have
carried no information and every group would need re-reading by hand.

**The Candidate identity had to change.** Extraction keyed Candidates on URL
alone and kept the longest anchor text, so a page listing several courses
collapsed to one Candidate before matching could see it. Identity is now
`(url, variant_stem(name))`.

**Coverage is bought at a visible price.** Denied rows carry
`share_denied_stem_mismatch` with the URL they wanted and the row holding it, so
a reviewer can override in one glance instead of meeting an unexplained blank.
