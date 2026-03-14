import time
from unittest.mock import MagicMock

from web_listening.blocks.scheduler import Scheduler


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
