"""Run logging: human-readable on the console, JSONL on disk for tracing.

Before this, 15 `print()` calls in `run.py` were the entire observability story.
That was survivable for 12 Institutions and is not for 2,220 sites: when a
site yields nothing, the question is always *which* filter rejected *what*, and
that has to be recoverable after the run rather than reproduced by re-crawling.

Three handlers, deliberately different:

  console        one line per site, for a human watching a long run
  <run_id>.log   the same human-readable lines, on disk
  <run_id>.jsonl one JSON object per line, carrying the structured fields
                 (`site_key`, `candidates`, `failure_reason`, ...) that make a
                 run greppable and machine-readable afterwards

Anything passed via `extra=` lands in the JSONL record and is omitted from the
console line, so structured detail never makes the console unreadable.

The plain-text file exists so the readable log lands in `logs/` whatever the
launch command does. Relying on a shell redirect put `stage1.log` in the repo
root, untracked and unignored, and a redirect cannot name the file after a
`run_id` that does not exist until the process starts.
"""

from __future__ import annotations

import contextvars
import glob
import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone

LOG_DIR = "logs"

# A `--verbose` run is projected at ~175 MB. Rotation here is a runaway guard,
# not routine size management: `RotatingFileHandler` discards the *oldest*
# segment, which for a single run means losing `load.done`, seed discovery and
# the early sites — usually the part worth keeping. The backup count is
# therefore generous enough (~1 GB) that a healthy run never rotates at all,
# and a run that does rotate says so.
MAX_LOG_BYTES = 50 * 1024 * 1024
LOG_BACKUPS = 20

# Which site a record belongs to. A ContextVar rather than a parameter because
# threading `site_key` through find_seeds, crawl_listings, the three harvest_*
# functions and assign would churn a dozen signatures to carry one string.
# Each pool worker sets it once and every record it emits inherits it.
_current_site: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_site", default="")


def set_current_site(site_key: str) -> None:
    """Tag every subsequent record from this thread with *site_key*."""
    _current_site.set(site_key)


class SiteContextFilter(logging.Filter):
    """Attach the current site to each record, so logs are filterable by site."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Always keeps the record; only annotates it."""
        site = _current_site.get()
        if site and not hasattr(record, "site_key"):
            record.site_key = site
        return True

# Attributes LogRecord always carries; anything else came from `extra=` and is
# therefore ours to emit.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class JsonlFormatter(logging.Formatter):
    """One JSON object per line, with `extra=` fields promoted to top level."""

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as a single JSON line."""
        payload = {
            "ts": datetime.fromtimestamp(
                record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_") or key == "message":
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# A run's files: <run_id>.jsonl, <run_id>.log, and any <run_id>.log.N backups.
_RUN_FILE = re.compile(r"^(?P<run>.+?)\.(?:jsonl|log)(?:\.\d+)?$")


def _runs_in(log_dir: str) -> dict[str, list[str]]:
    """Map run_id -> its files, so a run is pruned whole or not at all."""
    runs: dict[str, list[str]] = {}
    for path in glob.glob(os.path.join(log_dir, "*")):
        m = _RUN_FILE.match(os.path.basename(path))
        if m:
            runs.setdefault(m.group("run"), []).append(path)
    return runs


def prune_old_runs(log_dir: str = LOG_DIR, keep: int = 20,
                   current: str = "") -> list[str]:
    """Keep the newest *keep* previous runs; delete older ones.

    The current run is excluded from the count as well as from deletion, so a
    `keep` of 20 leaves 21 runs on disk during a run and 20 after it. Erring one
    high is deliberate: the alternative is a default that deletes more than the
    number it names. Returns the run ids removed.

    Logs are history, not cache: unlike `.cache/` they cannot be regenerated,
    so this errs generous and is skipped entirely when `keep` is 0.

    Two properties matter more than the count. A run is removed **whole** —
    JSONL, readable log and any rotation backups together — so none is left
    half-deleted with one form missing. And *current* is never removed, stated
    explicitly rather than relying on it sorting last.
    """
    if keep <= 0 or not os.path.isdir(log_dir):
        return []
    runs = _runs_in(log_dir)
    runs.pop(current, None)
    # Ordered by file mtime, deliberately not by name. run_ids are
    # timestamp-prefixed, but the readable format sorts *before* the older
    # compact one — "2026-09-02..." < "20260901..." because "-" precedes "0" —
    # so a lexical sort would delete the newest logs first whenever both
    # formats are present. mtime is what "oldest" actually means.
    def newest_mtime(run: str) -> float:
        """Most recent mtime among a run's files."""
        return max((os.path.getmtime(p) for p in runs[run]
                    if os.path.exists(p)), default=0.0)

    doomed = sorted(runs, key=newest_mtime)[:max(0, len(runs) - keep)]
    removed = []
    for run in doomed:
        for path in runs[run]:
            try:
                os.remove(path)
            except OSError:
                continue
        removed.append(run)
    return removed


def setup_logging(run_id: str, log_dir: str = LOG_DIR,
                  verbose: bool = False, quiet: bool = False,
                  keep_runs: int = 20) -> str:
    """Configure logging for one run. Returns the JSONL log path.

    Also writes `<log_dir>/<run_id>.log` with the human-readable lines, so both
    forms of the run are in `logs/` without depending on how it was launched.

    Idempotent: existing handlers are removed first, so calling this twice in
    one process (a test, a notebook) does not double every line.
    """
    os.makedirs(log_dir, exist_ok=True)
    # Prune before opening this run's handlers, so the new files are never
    # candidates for deletion and a crash mid-prune cannot orphan them.
    pruned = prune_old_runs(log_dir, keep=keep_runs, current=run_id)

    path = os.path.join(log_dir, f"{run_id}.jsonl")
    text_path = os.path.join(log_dir, f"{run_id}.log")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    site_filter = SiteContextFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonlFormatter())
    file_handler.addFilter(site_filter)
    root.addHandler(file_handler)

    text_handler = logging.handlers.RotatingFileHandler(
        text_path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    text_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    text_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(text_handler)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)

    if pruned:
        logging.getLogger(__name__).info(
            f"pruned {len(pruned)} old run log(s)",
            extra={"pruned_runs": len(pruned), "keep_runs": keep_runs})

    logging.getLogger(__name__).debug(
        "logging started",
        extra={"run_id": run_id, "log_file": path, "text_log": text_path})
    return path
