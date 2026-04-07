from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass(slots=True)
class PolitePacer:
    request_delay_ms: int = 0
    file_request_delay_ms: int | None = None
    request_jitter_ms: int = 0
    last_request_started_at: float | None = None

    @classmethod
    def from_config(cls, fetch_config_json: dict | None = None) -> "PolitePacer":
        config = fetch_config_json or {}
        request_delay_ms = int(config.get("request_delay_ms", 0) or 0)
        file_request_delay_raw = config.get("file_request_delay_ms")
        file_request_delay_ms = int(file_request_delay_raw) if file_request_delay_raw is not None else None
        request_jitter_ms = int(config.get("request_jitter_ms", 0) or 0)
        return cls(
            request_delay_ms=max(0, request_delay_ms),
            file_request_delay_ms=max(0, file_request_delay_ms) if file_request_delay_ms is not None else None,
            request_jitter_ms=max(0, request_jitter_ms),
        )

    def wait_for_request(self, request_kind: str = "page") -> None:
        now = time.monotonic()
        if self.last_request_started_at is None:
            self.last_request_started_at = now
            return

        base_delay_ms = self.request_delay_ms
        if request_kind == "file" and self.file_request_delay_ms is not None:
            base_delay_ms = self.file_request_delay_ms
        if base_delay_ms <= 0:
            self.last_request_started_at = now
            return

        jitter_ms = random.randint(0, self.request_jitter_ms) if self.request_jitter_ms > 0 else 0
        target_interval_ms = base_delay_ms + jitter_ms
        elapsed_ms = (now - self.last_request_started_at) * 1000
        remaining_ms = target_interval_ms - elapsed_ms
        if remaining_ms > 0:
            time.sleep(remaining_ms / 1000)
            now = time.monotonic()
        self.last_request_started_at = now
