import time
from datetime import timezone
from unittest.mock import MagicMock

from web_listening.blocks.scheduler import Scheduler


def test_scheduler_passes_explicit_utc_timezone(monkeypatch):
    """Scheduler startup does not rely on host-local timezone auto-detection."""

    captured: dict[str, object] = {}

    class FakeTrigger:
        def __init__(self, *, minutes, timezone):
            captured["minutes"] = minutes
            captured["timezone"] = timezone

    class FakeBackgroundScheduler:
        def __init__(self, *, timezone):
            captured["scheduler_timezone"] = timezone
            self.running = False

        def add_job(self, callback, *, trigger, id, replace_existing, next_run_time):
            captured["callback"] = callback
            captured["trigger"] = trigger
            captured["job_id"] = id
            captured["replace_existing"] = replace_existing
            captured["next_run_time"] = next_run_time

        def start(self):
            self.running = True

        def shutdown(self, wait=False):
            self.running = False
            captured["shutdown_wait"] = wait

    monkeypatch.setattr("web_listening.blocks.scheduler.IntervalTrigger", FakeTrigger)
    monkeypatch.setattr("web_listening.blocks.scheduler.BackgroundScheduler", FakeBackgroundScheduler)

    scheduler = Scheduler(check_callback=lambda: None)
    scheduler.start(interval_minutes=15)
    scheduler.stop()

    assert captured["scheduler_timezone"] is timezone.utc
    assert captured["minutes"] == 15
    assert captured["timezone"] is timezone.utc
    assert captured["job_id"] == "site_check"
    assert captured["replace_existing"] is True
    assert captured["next_run_time"].tzinfo is timezone.utc
    assert captured["shutdown_wait"] is False


def test_scheduler_calls_callback():
    """Scheduler invokes the callback at least once when started."""
    callback = MagicMock()
    scheduler = Scheduler(check_callback=callback)

    with scheduler:
        scheduler.start(interval_minutes=1)
        time.sleep(0.5)  # callback is triggered immediately on start

    callback.assert_called()


def test_scheduler_stop_is_idempotent():
    """Calling stop() multiple times does not raise."""
    scheduler = Scheduler(check_callback=lambda: None)
    scheduler.start(interval_minutes=60)
    scheduler.stop()
    scheduler.stop()  # should not raise


def test_scheduler_swallows_callback_exceptions():
    """Exceptions in the callback must not crash the scheduler thread."""
    call_count = {"n": 0}

    def bad_callback():
        call_count["n"] += 1
        raise RuntimeError("intentional error")

    scheduler = Scheduler(check_callback=bad_callback)
    with scheduler:
        scheduler.start(interval_minutes=1)
        time.sleep(0.5)

    assert call_count["n"] >= 1  # callback was invoked despite exceptions
