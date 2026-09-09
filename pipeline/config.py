"""Local run configuration, read from a `.env` file into the environment.

Why this exists
---------------
`SerperProvider` takes its key from `$SERPER_API_KEY` and never from a CLI
flag, because `run.py` writes `vars(args)` into the `runs` table as JSON and a
flag would persist the credential in `pipeline.db`. That is the right place for
it, but it left the key to be `export`ed by hand in every shell — easy to
forget, and it lingers in shell history.

Reading a git-ignored `.env` keeps the credential out of both the command line
and the database while surviving between sessions.

Why not `python-dotenv`
-----------------------
Not because a dependency was forbidden — ADR-0003 governs paid and
nondeterministic *services*, not pure-Python libraries, and `bs4`/`lxml` were
always compatible with it. It is that the features `python-dotenv` adds over
the parser below are interpolation and multi-line values, both of which are
deliberately unsupported here: this file holds credentials, and a `.env` that
can compute is a `.env` whose effective value has to be traced rather than
read.

What is deliberately not supported
----------------------------------
`${VAR}` interpolation, multi-line values, and precedence chains
(`.env.local` over `.env`). Each is easy to add when something needs it, and
each makes "what is my key actually set to" a harder question until then.
"""

from __future__ import annotations

import os

DEFAULT_ENV_FILE = ".env"


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` *text* into a mapping, skipping anything unparseable.

    A malformed line is skipped rather than raised on. Most runs need no
    credential at all, and a stray line in a local file is no reason to stop a
    Phase 1 crawl that was never going to read it.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # `export KEY=value` is the form people paste out of shell
        # instructions, so accept it rather than silently ignoring the line.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # One layer of matching quotes, so a value with deliberate leading or
        # trailing spaces can still be expressed.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path: str = DEFAULT_ENV_FILE) -> int:
    """Load *path* into `os.environ`, returning how many names it set.

    **The existing environment wins.** A name already set is left alone, so
    `SERPER_API_KEY=... python3 run.py ...` and a CI secret both still override
    the file — the standard precedence, and the one that makes a one-off run
    predictable.

    A missing file is success, returning 0: no vendor configured is the common
    case. Returns the count and never the names' values, so a caller cannot
    accidentally log a credential.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            values = parse_env(fh.read())
    except (OSError, UnicodeDecodeError):
        return 0

    loaded = 0
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
