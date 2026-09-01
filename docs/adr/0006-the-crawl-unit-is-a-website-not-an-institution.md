---
status: accepted
---

# The crawl unit is a website host, not an Institution

Everything downstream is bucketed per website host rather than per Institution:
one Catalog, one crawl budget, one rate-limit key, one Assignment.

This is surprising, because the domain language and the CSV are both organised
around Institutions. The reason is measured: **165 hosts are shared by 455
Institutions covering 12,927 Course Rows — 24.5% of the sheet.** They are
per-country instances of one provider (`British Council (Australia)`,
`(Bangladesh)`, `(Bhutan)`, … fifteen of them on `britishcouncil.org`) and
franchise groups sharing a site (eight Academies Australasia entities on
`academies.edu.au`).

Bucketing per Institution would crawl `britishcouncil.org` fifteen times under
fifteen separate page budgets, and resolve each instance against a fifteenth of
the available Candidates. Bucketing per host reduced 2,548 Institutions to 2,220
crawl units and lets every row on a site see every Candidate the site published.

It composes with ADR-0004 rather than fighting it: two rows named `IELTS` under
different Institution records on the same host have the same Variant Stem, so
they are siblings and legitimately share the page — which is the right answer.

## Consequences

**A bucket needs a display name.** `site_display_name()` picks the most common
Institution name in the bucket, used for reporting and for stripping the
Institution's own name out of page titles during scoring.

**Reporting stays per-Institution.** The coverage report still groups by
Institution, because that is what a human reasons about; only crawling and
Assignment are host-scoped.

**Grouping depends on `registrable()` being right.** It was not: it collapsed
`barkly.vic.edu.au` to `vic.edu.au`, which put 144 unrelated Victorian schools
in one bucket. Harmless while the value was only a rate-limit key, actively
wrong once it selects a Catalog. Fixed with an explicit list of three-label
public suffixes — deliberately a list, since `.taylors.edu.my` and `.bcu.ac.uk`
show that a general "three short labels" rule fails in the other direction.
