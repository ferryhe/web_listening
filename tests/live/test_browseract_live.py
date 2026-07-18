from __future__ import annotations

import os
import sys

import pytest

from web_listening.executors.browseract import inspect_browseract


@pytest.mark.skipif(not os.environ.get("WEB_LISTENING_BROWSERACT_LIVE"), reason="opt-in BrowserAct runtime smoke")
def test_browseract_live_handshake_only():
    executable = os.environ.get("WEB_LISTENING_BROWSERACT_EXECUTABLE")
    assert executable, "set WEB_LISTENING_BROWSERACT_EXECUTABLE to an absolute path"
    inspection = inspect_browseract(executable, project_prefix=sys.prefix)
    assert inspection["available"], inspection["errors"]
