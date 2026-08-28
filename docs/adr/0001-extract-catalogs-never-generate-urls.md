---
status: accepted
---

# Extract Catalogs from institution listings, never generate URLs

A reader will reasonably ask why we crawl listing pages instead of slugifying
course names into URL patterns — pattern generation would be vastly cheaper than
crawling 559 Sites. We measured it and it is disqualified, not merely risky.

## Considered Options

**Generate URLs from course names.** Tested against Aberystwyth, whose pattern
looked obvious (`courses.aber.ac.uk/undergraduate/data-science/`). One of six
guesses hit. The real slugs include `142L-international-relations`,
`BA-education-spanish` and `D406D-nuffield`, which no generator produces.

Worse, the failures are silent rather than loud. `/undergraduate/equine-science/`
returns **404**, but `/postgraduate/equine-science/` returns **200** — so a
generator asked for an undergraduate course receives a live, plausible,
*wrong-Level* page. At 10,637 rows nobody eyeballs that, and a wrong URL is
undetectable downstream in a way a blank never is.

**Extract Candidates from the Institution's own listings.** Chosen. The
pipeline can only ever select a URL that the Site itself published next to a
course name, which makes the wrong-Level failure structurally impossible.

## Consequences

Some Sites are JavaScript course-finders that publish nothing in their HTML —
Abertay's `course-search` is 148KB yielding 13 nav links. Those Institutions
must fail closed to `no_catalog` rather than fall back to generation. Accepting
lower coverage on those Sites is the price of never emitting a confident wrong
URL, and it is the trade we chose deliberately.
