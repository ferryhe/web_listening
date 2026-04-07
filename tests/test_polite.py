from web_listening.blocks.polite import PolitePacer


def test_polite_pacer_waits_for_remaining_delay(monkeypatch):
    observed_sleeps: list[float] = []
    moments = iter([0.0, 0.2, 1.0])

    monkeypatch.setattr("web_listening.blocks.polite.time.monotonic", lambda: next(moments))
    monkeypatch.setattr("web_listening.blocks.polite.time.sleep", lambda seconds: observed_sleeps.append(seconds))

    pacer = PolitePacer(request_delay_ms=1000)
    pacer.wait_for_request("page")
    pacer.wait_for_request("page")

    assert observed_sleeps == [0.8]


def test_polite_pacer_uses_file_override_and_jitter(monkeypatch):
    observed_sleeps: list[float] = []
    moments = iter([0.0, 0.5, 1.7])

    monkeypatch.setattr("web_listening.blocks.polite.random.randint", lambda start, end: 200)
    monkeypatch.setattr("web_listening.blocks.polite.time.monotonic", lambda: next(moments))
    monkeypatch.setattr("web_listening.blocks.polite.time.sleep", lambda seconds: observed_sleeps.append(seconds))

    pacer = PolitePacer(request_delay_ms=1000, file_request_delay_ms=1200, request_jitter_ms=500)
    pacer.wait_for_request("page")
    pacer.wait_for_request("file")

    assert observed_sleeps == [0.9]


def test_polite_pacer_builds_from_fetch_config():
    pacer = PolitePacer.from_config(
        {
            "request_delay_ms": 1200,
            "file_request_delay_ms": 2500,
            "request_jitter_ms": 600,
        }
    )

    assert pacer.request_delay_ms == 1200
    assert pacer.file_request_delay_ms == 2500
    assert pacer.request_jitter_ms == 600
