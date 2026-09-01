"""Durable append and progress reporting for the fetch lane. Two small helpers, kept
free of any other dependency on purpose.

`append` flushes and fsyncs every row. Judging is the most expensive step in this
pipeline, and cells already judged must not have to be redone because the process was
killed midway. The lock is there because several threads write the same file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

WRITE_LOCK = Lock()


def append(path: Path, rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def progress(done: int, total: int, t0: float, *, every: int = 10) -> str | None:
    """A progress line, or None when this call should not print:
    `if (s := progress(...)): print(s)`.

    A long run has to be observable, or a real error scrolls past unseen.
    """
    if done % every and done != total:
        return None
    el = time.time() - t0
    eta = el / max(1, done) * (total - done)
    return "  %d/%d  %.1f min elapsed  %.1f min remaining" % (done, total, el / 60, eta / 60)
