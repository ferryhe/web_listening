"""Background scheduler for periodic site checks.

Built on `APScheduler <https://apscheduler.readthedocs.io/>`_ (3.x), a
production-grade Python scheduling library used by many open-source projects.

Usage::

    from web_listening.blocks.scheduler import Scheduler

    def my_check():
        # perform the check logic here
        ...

    scheduler = Scheduler(check_callback=my_check)
    with scheduler:
        scheduler.start(interval_minutes=60)
        # block or serve until interrupted

The scheduler runs in a background thread so it does not block the main
process (useful when embedded alongside the FastAPI server).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_JOB_ID = "site_check"


class Scheduler:
    """Periodically invoke a user-supplied callback using APScheduler.

    Args:
        check_callback: Zero-argument callable invoked on each scheduled tick.
    """

    def __init__(self, check_callback: Callable[[], None]) -> None:
        self._callback = check_callback
        self._scheduler = BackgroundScheduler(timezone=timezone.utc)

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, interval_minutes: int = 60) -> None:
        """Start background scheduling.

        The callback is executed immediately upon start and then every
        *interval_minutes* minutes thereafter.

        Args:
            interval_minutes: How often to invoke the callback (default: 60).
        """
        self._scheduler.add_job(
            self._run,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=_JOB_ID,
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
        )
        self._scheduler.start()
        logger.info("Scheduler started (interval=%d min)", interval_minutes)

    def stop(self) -> None:
        """Shut down the background scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    # ── Context-manager support ─────────────────────────────────────────────

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    # ── Internal ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._callback()
        except Exception as exc:
            logger.exception("Scheduled check failed: %s", exc)
