"""Runs are processed by a small pool of worker threads, whatever started them (upload, sample or email).

A pool rather than one thread per request keeps a burst of emails from firing dozens of model calls
at once, which on a free AI tier only produces rate-limit errors.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)


class RunQueue:
    def __init__(self, work: Callable[[int], None], workers: int) -> None:
        self._work = work
        # workers=0 runs each job on the caller's thread, which is what the tests want.
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run") if workers > 0 else None

    def submit(self, run_id: int) -> None:
        if self._pool is None:
            self._run(run_id)
        else:
            self._pool.submit(self._run, run_id)

    def _run(self, run_id: int) -> None:
        try:
            self._work(run_id)
        except Exception:  # the pipeline records its own failures; this is only so a worker never dies silently
            logger.exception("Run %s crashed outside the pipeline", run_id)

    def shutdown(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
