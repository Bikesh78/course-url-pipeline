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
A provider of courses, keyed by `institution_name`, owning exactly one Site.
_Avoid_: university, provider, school, organisation

**Site**:
The institution's own web presence, rooted at a canonical host. A URL that is
not on the institution's Site is never a valid result.
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
One `(course name, URL)` pair from a Catalog, considered as a possible match
for a Course Row.
_Avoid_: option, result, hit

**Assignment**:
The constrained selection of at most one Candidate per Course Row, where each
URL may be claimed by at most one Course Row within an Institution. The
uniqueness constraint is what makes Assignment different from independent
per-row matching.
_Avoid_: matching, allocation, mapping

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

**Prior Note**:
An annotation left in `Notes` or `course_url_via_web_search` by an earlier
manual attempt. Treated as evidence to cross-check against, never as authority
— these notes are known to contain false `not found` verdicts.
_Avoid_: existing data, human input, ground truth
