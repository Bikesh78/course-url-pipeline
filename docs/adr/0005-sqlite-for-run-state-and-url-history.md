---
status: accepted
---

# SQLite holds run state and URL history; CSV stays the deliverable

Course URLs drift. An Institution restructures its catalogue and a URL that
resolved last quarter 404s this one, so a result is a snapshot with a date
rather than a permanent fact. Answering "what URL did this course have in June"
from CSVs means keeping and diffing 52,781-row files by hand, and nothing
records *why* a URL changed.

`pipeline.db` therefore holds runs, sites, catalogs, per-row results, and an
append-only `url_history`. The CSVs are generated from it, so the output
contract is unchanged.

A reader may reasonably ask why a database appears in an otherwise entirely
file-based project. Two things make it cheap: `sqlite3` is in the standard
library, so ADR-0003's no-pip-install guarantee is untouched; and writes happen
on the main thread only — `run.py` collects worker results in its
`as_completed` loop — so there is no writer queue and no locking design to get
wrong.

## Considered and rejected

**Timestamp columns on the CSV only.** Simplest, and answers "when was this
last checked". Cannot answer what a URL used to be, which was the actual
requirement.

**An append-only JSONL history file.** Keeps the project file-based and stays
greppable, but reconstructing current state means replaying the log, and there
are no indexes — which is precisely the work a database does at this row count.

## Consequences

**The page cache deliberately stays on disk.** `.cache/` holds gzipped
responses and is what makes reruns nearly free; moving several hundred thousand
entries into SQLite would risk that property and buy no coverage. The store
holds *decisions*, not *pages*.

**`url_history` counts distinct URLs, not runs.** Re-running with an unchanged
result refreshes `last_seen` rather than appending, so the number of history
rows for a course is the number of different URLs it has held — which is the
question worth asking.
