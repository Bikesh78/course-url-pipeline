# Course URL Resolution

This project resolves a public, institution-hosted web page for each course row
in `processed_courses.csv`, filling the `course_url` column with a confidence
score so that human attention is spent only on the ambiguous minority.

## Language

### The data

**Course Row**:
One line of the input CSV, identified by its `id`. The unit of work — every
decision the pipeline makes is scoped to a single course row.
_Avoid_: record, entry, item

**Institution**:
A provider of courses, keyed by `institution_id`. The unit of *reporting*, not
of crawling — several Institutions may share one Site.
_Avoid_: university, provider, school, organisation

**Site**:
A web presence rooted at one canonical host, and **the unit of crawling** — not
the Institution. Several Institutions can share one Site: 165 hosts are shared
by 455 Institutions. A URL that is not on the Site is never a valid result.
_Avoid_: website, domain, homepage

**Award**:
The credential a course confers — `BSc`, `MSc`, `MBA`, `FdSc`. Distinct from
the subject, and load-bearing: two rows may share a subject and differ only by
Award.
_Avoid_: degree, qualification, level

**Level**:
Whether a course is undergraduate or postgraduate. Derived from the Award, and
used to constrain which part of a Catalog a Course Row may match against.
_Avoid_: tier, stage

### Resolution

**Catalog**:
The set of Candidates extracted from an Institution's own listing pages. A
Catalog is always *extracted* and never *generated* — the pipeline does not
construct URLs.
_Avoid_: index, course list, sitemap

**Candidate**:
One `(course name, URL)` pair from a Catalog, considered as a possible match for
a Course Row. Its identity is the URL *plus* the Variant Stem of its name, so
one page listing several courses yields several Candidates.
_Avoid_: option, result, hit

**Assignment**:
The constrained selection of at most one Candidate per Course Row. A URL may be
claimed by several Course Rows, but only when they are Variant Siblings and only
up to a cap — what makes Assignment different from matching each row
independently.
_Avoid_: matching, allocation, mapping

**Variant Stem**:
A course's subject identity, with its Award and any delivery variant removed.
`Anthropology BA (Hons)` and `Anthropology with Placement BA (Hons)` share the
Variant Stem `anthropology`; `Archaeology and Anthropology BA (Hons)` does not.
_Avoid_: subject, base name, root

**Variant Sibling**:
One of two or more Course Rows sharing a Variant Stem and an Award class — the
same course delivered differently. The only relationship that permits two rows
to hold one URL.
_Avoid_: duplicate, variant, pair

**Share Group**:
The set of Course Rows holding one URL. A Share Group always means one course in
several variants; anything else is refused rather than filled.
_Avoid_: cluster, collision, group

**Score**:
Normalised similarity between a Course Row's name and its Candidate's name, on
0.000–1.000. A measure of *how alike the names are*.
_Avoid_: confidence, similarity, rating

**Margin**:
The assigned Candidate's Score minus the best rejected Candidate's Score. The
measure of *how ambiguous the choice was* — deliberately separate from Score,
because a high Score with a low Margin is untrustworthy. A **negative** Margin
means the Course Row was displaced: something it scored higher against was
claimed by a stronger Assignment, so it holds a second choice.
_Avoid_: delta, gap, difference

**URL History**:
The sequence of URLs one Course Row has held over time, with when each was first
and last seen. Exists because a resolved URL is a snapshot with a date rather
than a permanent fact — Institutions restructure their catalogues.
_Avoid_: audit log, versions, changelog

**Run**:
One execution of the pipeline, identified by a `run_id` that stamps every log
record and every stored result. The unit of reproducibility.
_Avoid_: job, batch, execution

**Verification**:
Fetching an assigned URL and re-scoring the live page's title against the
Course Row's name. What distinguishes a `verified` result from a merely
`probable` one.
_Avoid_: validation, checking, confirmation

**Extraction Health**:
Whether an Institution's Catalog is plausibly complete, judged by comparing
Candidate count against that Institution's Course Row count. A failing
Extraction Health escalates the Institution rather than emitting weak matches.
_Avoid_: quality, completeness, coverage

**Blocked Site**:
An Institution whose Site refuses automated access, answering refusal statuses
to every probe rather than serving pages. Distinct from a Site that merely
defeated extraction: a Blocked Site cannot be reached by better crawling at
all, so it is a question of access permission rather than of technique.
_Avoid_: failed, unreachable, broken, 403

**Mistargeted Crawl**:
An extraction that passed Extraction Health yet resolved almost no Course Rows,
because the Candidates it gathered came from the wrong part of the Site. The
opposite failure to a thin Catalog — enough was found, but not the right
things.
_Avoid_: bad crawl, extraction suspect, low yield

### Review

**Review Queue**:
The subset of Course Rows whose Score or Margin falls short of automatic
acceptance, surfaced for human triage worst-first.
_Avoid_: manual queue, exceptions, failures

**Row Flag**:
A reason code attached to a Course Row recording something known about it that
explains a weak or absent result — that its Institution is a primary school,
that its name is a bare credential, that its source line was malformed.
_Avoid_: tag, label, warning

**Prior URL**:
A URL an earlier pipeline associated with a Course Row, carried in the input
sheet. Used as a crawl seed and, once the page has been read for its own
heading, as a Candidate. It may be adopted as the answer when it beats ours,
but it is never *assumed* to be the answer, and it is always preserved in the
output rather than overwritten.
_Avoid_: existing URL, old match, given URL

**Carried Over**:
A Course Row whose delivered URL came from the source sheet rather than from
extraction, because ours was absent, dead, or beaten by it.
_Avoid_: reused, inherited, kept

**Search Candidate**:
A URL a search provider proposed. It has earned nothing by being proposed: it
is scored, domain-checked and put through the sharing rule like any other
Candidate, and can never be reported Verified on a search ranking alone.
_Avoid_: search result, hit, top result

**Prior Note**:
An annotation left in `Notes` or `course_url_via_web_search` by an earlier
manual attempt. Treated as evidence to cross-check against, never as authority
— these notes are known to contain false `not found` verdicts.
_Avoid_: existing data, human input, ground truth
