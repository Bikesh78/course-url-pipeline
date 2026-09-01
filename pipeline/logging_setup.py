"""Run logging: human-readable on the console, JSONL on disk for tracing.

Before this, 15 `print()` calls in `run.py` were the entire observability story.
That was survivable for 12 Institutions and is not for 2,220 sites: when a
site yields nothing, the question is always *which* filter rejected *what*, and
that has to be recoverable after the run rather than reproduced by re-crawling.

Two handlers, deliberately different:

  console  one line per site, for a human watching a long run
  file     one JSON object per line in `logs/<run_id>.jsonl`, carrying the
           structured fields (`site_key`, `candidates`, `diagnosis`, ...) that
           make a run greppable and machine-readable afterwards

Anything passed via `extra=` lands in the JSONL record and is omitted from the
console line, so structured detail never makes the console unreadable.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

LOG_DIR = "logs"

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


def setup_logging(run_id: str, log_dir: str = LOG_DIR,
                  verbose: bool = False, quiet: bool = False) -> str:
    """Configure logging for one run. Returns the log file path.

    Idempotent: existing handlers are removed first, so calling this twice in
    one process (a test, a notebook) does not double every line.
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{run_id}.jsonl")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonlFormatter())
    root.addHandler(file_handler)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)

    logging.getLogger(__name__).debug(
        "logging started", extra={"run_id": run_id, "log_file": path})
    return path
