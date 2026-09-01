"""Reading, repairing and flagging the input CSV.

`processed_courses.csv` is treated as strictly read-only (see README). Anything
this module discovers about a Course Row is expressed as a Row Flag rather than
by editing the source.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from pipeline.fetch import registrable
from pipeline.normalize import normalize_name

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# The legacy sheet, kept readable via --input for comparison runs. Its ids are
# a strict subset of the current sheet's.
LEGACY_COLUMNS = ["id", "name", "institution_name", "course_url", "Notes",
                  "course_url_via_web_search", "matched_score",
                  "matched_status", "website"]

# `final_courses.csv`: 52,781 rows over 2,548 Institutions, properly quoted, so
# it needs no structural repair. It carries prior work — course_url on 28,835
# rows and program_link on 7,968 — which this pipeline treats as Candidate
# evidence, never authority (ADR-0004 decision 1).
FINAL_COLUMNS = ["id", "name", "institution_name", "course_url",
                 "course_url_via_web_search", "matched_score",
                 "matched_status", "is_processed", "web_search_status",
                 "institution_id", "website", "program_link", "mongodb_id",
                 "batch_id", "processed_date"]

INPUT_COLUMNS = LEGACY_COLUMNS      # what write_filled_csv still emits

DEFAULT_INPUT = "final_courses.csv"

# Names that are a credential with no subject. 132 such rows.
BARE_CREDENTIALS = {
    "ielts", "pte", "toefl", "toeic", "ossd", "sace", "elicos", "eap",
    "mba", "gre", "gmat", "cae", "fce", "celta", "delta", "hsc", "vce",
    "atar", "ib", "gcse", "a level", "a levels", "foundation",
    "general english", "english", "academic english", "esl",
}

_K12_INSTITUTION = re.compile(
    r"\b(primary\s+school|secondary\s+college|secondary\s+school|high\s+school"
    r"|infants?\s+school|p-\d+\s+college|primary\s+&?\s*secondary)\b",
    re.IGNORECASE)
_YEAR_LEVEL = re.compile(
    r"^\s*(year|grade|prep|kindergarten|kinder|foundation\s+year)\b"
    r"|^\s*year\s+level\s*:", re.IGNORECASE)
# ANZSCO occupation codes from the Department of Home Affairs, e.g.
# "Aboriginal and Torres Strait Islander Health Worker - 411511 (subclass 186)".
# 5,083 rows (9.6% of the sheet) are skilled-migration occupations rather than
# courses, and only 33 of them carry any URL. Flagging them keeps a reviewer
# from reading 5,083 blanks as a crawler failure.
_OCCUPATION_CODE = re.compile(r"\(\s*subclass\s*\d+\s*\)|\b\d{6}\b\s*\(", re.IGNORECASE)

_AGGREGATOR = re.compile(
    r"\b(shorelight|studygroup|study-group|navitas|kaplan|idp|"
    r"educations?\.com|hotcourses|studyportals)\b", re.IGNORECASE)
_URL_IN_TEXT = re.compile(r"https?://[^\s,\"']+")


@dataclass
class CourseRow:
    """One line of the input CSV: the unit of work, identified by `id`.

    The demand side of the match — Candidates are the supply side. See
    `CONTEXT.md` for the domain definition.

    A Course Row is a *claim* that a course exists, not proof of one. Some are
    unsatisfiable no matter how well the pipeline runs: "Equine Science BSc
    (Hons)" is a well-formed row for a degree Aberystwyth has withdrawn, and
    "YEAR 2" at Aintree Primary School was never a course. `no_match` is the
    correct answer for those, and no amount of crawling or spending changes it.

    `id` is never modified. The pipeline reads it, writes it back, and hangs
    `course_url` off it — that is the mapping the output exists to provide.
    """

    id: str
    name: str
    institution_name: str
    website: str
    notes: str = ""
    prior_web_search: str = ""
    flags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    # Present only in the current sheet.
    institution_id: str = ""
    # A URL an earlier pipeline assigned to this row. Fed in as a seed and a
    # Candidate because it reveals the Site's URL shape for free, but never
    # trusted: 60% of such rows shared a URL with another course, including one
    # page holding 116 unrelated courses.
    prior_course_url: str = ""
    program_link: str = ""
    batch_id: str = ""
    processed_date: str = ""

    @property
    def prior_note_url(self) -> str:
        """A URL a previous manual pass left behind, if it left one."""
        for text in (self.prior_web_search, self.notes):
            m = _URL_IN_TEXT.search(text or "")
            if m:
                return m.group(0)
        return ""

    @property
    def prior_said_not_found(self) -> bool:
        """Did an earlier manual pass record "not found" for this row?

        Evidence, never authority. 222 rows carry this verdict and it is known
        to be wrong often — 58 rows in the last run got a URL despite it. It is
        reported as `prior_note_disagrees`, which measures what the manual
        attempt missed, and it never causes a row to be skipped.
        """
        return (self.notes or "").strip().lower() == "not found" or \
               (self.prior_web_search or "").strip().lower() == "not found"

    @property
    def site_host(self) -> str:
        """Registrable host of this row's Institution website.

        The **crawl unit**, deliberately not the Institution. 165 hosts are
        shared by 455 Institutions covering 12,927 rows — per-country instances
        of one provider ("British Council (Bangladesh)", "(Bhutan)", ...) and
        franchise groups. Crawling per Institution would fetch
        britishcouncil.org fifteen times and build fifteen partial Catalogs.
        """
        return _host_of(self.website)

    @property
    def institution_key(self) -> str:
        """Stable Institution identity: the id where present, else the name.

        Two Institutions in the current sheet differ only in the casing of
        their name, so lower-casing the name silently merges them; the id does
        not.
        """
        return self.institution_id or self.institution_name.strip().lower()

    @property
    def prior_urls(self) -> list[str]:
        """URLs earlier work associated with this row, best first."""
        out = []
        for u in (self.prior_course_url, self.program_link,
                  self.prior_note_url):
            u = (u or "").strip()
            if u.startswith(("http://", "https://")) and u not in out:
                out.append(u)
        return out

    @property
    def work_key(self) -> tuple[str, str]:
        """Rows sharing this key are the same piece of work.

        Deliberately conservative: only names that are textually identical
        (ignoring case, whitespace and trailing punctuation) are treated as one
        course. Using the full match normalisation here merged names that are
        plausibly distinct degrees — "Fine Art with Art History BA (Hons)" and
        "Fine Art/Art History BA (Hons)" collapse under it, because "with" is a
        stopword, yet at UK institutions "X with Y" (major/minor) and "X/Y"
        (joint honours) are frequently separate awards. Letting such rows
        compete instead forces the conflict into the Review Queue rather than
        silently assuming they are the same course.
        """
        name = re.sub(r"\s+", " ", self.name.strip().lower()).strip(" .,;:-")
        return (self.institution_key, name)


def _host_of(site: str) -> str:
    """Registrable host for grouping, or "" when the website is unusable."""
    site = normalise_website(site)
    if not site:
        return ""
    m = re.match(r"https?://([^/]+)", site)
    return registrable(m.group(1)) if m else ""


def _balanced(text: str) -> bool:
    return text.count("(") <= text.count(")")


MAX_MERGE = 5


def _strip_join_artefacts(name: str) -> str:
    return re.sub(r"[,\s]+$", "", name).strip()


def _repair_split_row(fields: list[str], known: set[str],
                      host_to_institution: dict[str, str],
                      ) -> tuple[list[str], bool, str]:
    """Rejoin a name that an unquoted comma split across columns.

    The source writer did not quote course names containing commas, so
    "Graduate Non-Award (Economics and Commerce, Visual Arts and Music)" became
    two fields and shifted every later column.

    Detection is anchored on the Institution registry, *not* on parenthesis
    balance. Many names legitimately carry an unbalanced paren — "Journalism
    (Politics BA (Hons)", "Doctoral Degree Geography (Arts" — and treating
    those as structural damage corrupts 38 otherwise-clean rows.

    Returns (fields, repaired, inferred_institution).
    """
    if len(fields) < 4 or fields[2].strip().lower() in known:
        return fields, False, ""

    # Anchor A: the Institution name itself was pushed past column 2. Merge
    # until it lands back in place.
    target = next((v.strip().lower() for v in fields[3:]
                   if v.strip().lower() in known), None)
    if target is not None:
        merged = list(fields)
        for _ in range(MAX_MERGE):
            if merged[2].strip().lower() == target:
                merged[1] = _strip_join_artefacts(merged[1])
                return merged, True, ""
            if len(merged) <= 3:
                break
            merged[1] = merged[1] + "," + merged[2]
            del merged[2]
        return fields, False, ""

    # Anchor B: no Institution name survived, but a URL in the row identifies
    # the Site. Here parenthesis balance is a sound *terminator* — we already
    # know from the failed registry lookup that the row is damaged, and the
    # split happened inside the parenthesis.
    for value in fields:
        m = _URL_IN_TEXT.search(value or "")
        if not m:
            continue
        host = re.sub(r"^www\.", "",
                      re.sub(r"^https?://", "", m.group(0)).split("/")[0].lower())
        inst = host_to_institution.get(host)
        if not inst:
            continue
        merged = list(fields)
        for _ in range(MAX_MERGE):
            if _balanced(merged[1]) or len(merged) <= 3:
                break
            merged[1] = merged[1] + "," + merged[2]
            del merged[2]
        merged[1] = _strip_join_artefacts(merged[1])
        return merged, True, inst
    return fields, False, ""


def load_rows(path: str = DEFAULT_INPUT) -> list[CourseRow]:
    """Load, repair and flag every Course Row. Never writes to *path*.

    Handles both sheets. The current one (`final_courses.csv`) is properly
    quoted and carries `institution_id` and `program_link`; the legacy one
    needs the structural repair below because its writer left commas inside
    unquoted course names.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        raw_rows = list(csv.reader(fh))
    if not raw_rows:
        return []

    if "institution_id" in raw_rows[0]:
        return _load_final(raw_rows)

    body = raw_rows[1:]

    # First pass: collect the Institution names and websites that parsed
    # cleanly, so repaired rows can have their Institution recovered.
    # The registry must exclude the corrupt rows' own debris, or those rows
    # look "already aligned" and never get repaired: their column 2 holds
    # fragments like " IT" and " Asian Studies". A value earns registry
    # membership only if corroborated — it appears in column 2 more than once,
    # or that row carries a plausible website. The 4 corrupt fragments appear
    # once each with no usable website, so they are excluded; the 77
    # single-course Institutions all have real websites, so they are kept.
    col2_counts: dict[str, int] = defaultdict(int)
    for f in body:
        if len(f) > 2 and f[2].strip():
            col2_counts[f[2].strip().lower()] += 1

    known_institutions: set[str] = set()
    website_by_institution: dict[str, str] = {}
    host_to_institution: dict[str, str] = {}
    for f in body:
        if len(f) <= 8 or not f[2].strip():
            continue
        inst = f[2].strip()
        site = (f[8] or "").strip()
        has_site = bool(re.match(r"^(https?://|www\.)", site))
        if col2_counts[inst.lower()] > 1 or has_site:
            known_institutions.add(inst.lower())
        if has_site and inst not in website_by_institution:
            website_by_institution[inst] = site
            m = re.search(r"https?://([^/]+)", site)
            if m:
                host_to_institution.setdefault(
                    re.sub(r"^www\.", "", m.group(1).lower()), inst)

    rows: list[CourseRow] = []
    for f in body:
        fields, repaired, inferred_inst = _repair_split_row(
            f, known_institutions, host_to_institution)
        fields = fields + [""] * (9 - len(fields))
        rec = dict(zip(INPUT_COLUMNS, fields[:9]))
        flags: list[str] = []

        if repaired:
            flags.append("malformed_row_repaired")
            # Merging realigned column 2, but the columns after it are still
            # shifted, so take the Institution's canonical website from the
            # registry rather than trusting this row's own column 8 (which
            # holds a stray institution UUID in the observed cases).
            inst = inferred_inst or rec["institution_name"].strip()
            rec["institution_name"] = inst
            rec["website"] = website_by_institution.get(inst, "")
            rec["Notes"] = " | ".join(v for v in fields[3:] if v.strip())

        row = CourseRow(
            id=(rec.get("id") or "").strip(),
            name=(rec.get("name") or "").strip(),
            institution_name=(rec.get("institution_name") or "").strip(),
            website=(rec.get("website") or "").strip(),
            notes=(rec.get("Notes") or "").strip(),
            prior_web_search=(rec.get("course_url_via_web_search") or "").strip(),
            flags=flags,
            raw=rec,
        )
        _apply_flags(row)
        rows.append(row)
    return rows


def _apply_flags(row: CourseRow) -> None:
    """Attach Row Flags explaining why a weak or absent result is expected."""
    if _K12_INSTITUTION.search(row.institution_name):
        row.flags.append("k12_institution")
    if _YEAR_LEVEL.search(row.name):
        row.flags.append("year_level_not_course")
    subject = normalize_name(row.name, drop_awards=True)
    if not subject or row.name.strip().lower() in BARE_CREDENTIALS:
        row.flags.append("bare_credential_name")
    if _OCCUPATION_CODE.search(row.name):
        row.flags.append("occupation_code_not_course")
    if not _balanced(row.name):
        # A name the source itself truncated mid-parenthesis ("Doctoral Degree
        # Geography (Arts"). It will match poorly and that is the data's fault,
        # not the matcher's — worth telling the reviewer.
        row.flags.append("truncated_name")
    if _AGGREGATOR.search(row.website):
        row.flags.append("aggregator_website")
    if not row.website or not re.match(r"^(https?://|www\.)", row.website):
        row.flags.append("unusable_website")


def _load_final(raw_rows: list[list[str]]) -> list[CourseRow]:
    """Load the current sheet. No structural repair needed — it is quoted."""
    header = [h.strip() for h in raw_rows[0]]
    rows: list[CourseRow] = []
    for fields in raw_rows[1:]:
        if not any(f.strip() for f in fields):
            continue
        rec = dict(zip(header, fields))
        row = CourseRow(
            id=(rec.get("id") or "").strip(),
            name=(rec.get("name") or "").strip(),
            institution_name=(rec.get("institution_name") or "").strip(),
            website=(rec.get("website") or "").strip(),
            notes="",
            prior_web_search=(rec.get("course_url_via_web_search") or "").strip(),
            raw=rec,
            institution_id=(rec.get("institution_id") or "").strip(),
            prior_course_url=(rec.get("course_url") or "").strip(),
            program_link=(rec.get("program_link") or "").strip(),
            batch_id=(rec.get("batch_id") or "").strip(),
            processed_date=(rec.get("processed_date") or "").strip(),
        )
        _apply_flags(row)
        rows.append(row)
    return rows


def incoming_score_is_foreign(value: str) -> bool:
    """Is this a score from the earlier pipeline rather than one of ours?

    The current sheet's `matched_score` runs 0-100; ours is 0-1. A value above
    1.0 must never be read as our confidence, and this guard exists so that a
    future reader who wires the column up gets a loud answer instead of 87.1
    silently meaning "certain".
    """
    try:
        return float(value) > 1.0
    except (TypeError, ValueError):
        return False


def normalise_website(site: str) -> str:
    """Make a `website` value fetchable: add a scheme, prefer https."""
    site = (site or "").strip()
    if not site:
        return ""
    if site.startswith("www."):
        site = "https://" + site
    if not site.startswith(("http://", "https://")):
        return ""
    if site.startswith("http://"):
        site = "https://" + site[len("http://"):]
    return site


def group_by_institution(rows: list[CourseRow]) -> dict[str, list[CourseRow]]:
    """Bucket Course Rows by Institution name. Retained for reporting only.

    Not the crawl unit — see `group_by_site`.
    """
    out: dict[str, list[CourseRow]] = defaultdict(list)
    for r in rows:
        out[r.institution_name].append(r)
    return dict(out)


def group_by_site(rows: list[CourseRow]) -> dict[str, list[CourseRow]]:
    """Bucket Course Rows by website host — the unit of parallelism.

    Everything downstream is site-scoped: one Catalog, one crawl budget, one
    rate-limit key, and one Assignment whose sharing rule holds within the
    bucket.

    The bucket is the **host**, not the Institution, because 165 hosts are
    shared by 455 Institutions covering 12,927 rows (24.5%). Those are
    per-country instances of one provider and franchise groups sharing a site.
    Bucketing per Institution would crawl britishcouncil.org fifteen times and
    resolve each instance against a fifteenth of the available Candidates.

    Rows with no usable website are bucketed under "" and reported
    `no_website`; no crawl can help them.
    """
    out: dict[str, list[CourseRow]] = defaultdict(list)
    for r in rows:
        out[r.site_host].append(r)
    return dict(out)


def site_display_name(rows: list[CourseRow]) -> str:
    """A human label for a site bucket: its most common Institution name."""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.institution_name:
            counts[r.institution_name] += 1
    if not counts:
        return rows[0].site_host if rows else ""
    return max(sorted(counts), key=lambda n: counts[n])


def dedupe(rows: list[CourseRow]) -> dict[tuple[str, str], list[CourseRow]]:
    """Collapse Course Rows that are the same work into one item."""
    out: dict[tuple[str, str], list[CourseRow]] = defaultdict(list)
    for r in rows:
        out[r.work_key].append(r)
    return dict(out)
