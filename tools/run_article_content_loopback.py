"""Offline Issue #70 smoke: real loopback HTTP, compiled governed readers."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.article_content.loopback import BODY, loopback_authority
from web_listening.blocks.article_content import fetch_article_content
from web_listening.config import settings


def main() -> int:
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="article-content-loopback-") as temporary:
        root = Path(temporary)
        with patch.object(settings, "data_dir", root):
            for scenario, expected in (
                ("success", "present"),
                ("fallback", "present"),
                ("all_empty", "no_content"),
                ("unrenderable", "no_content"),
                ("not_found", "not_found"),
            ):
                with loopback_authority(root / scenario, scenario) as (kwargs, seen):
                    result = fetch_article_content(**kwargs)
                    assert result.data_status == expected, result.model_dump_json()
                    if result.has_data:
                        body = (
                            Path(kwargs["output_dir"]) / result.data["content_ref"]
                        ).read_bytes()
                        assert body == BODY.encode()
                        assert hashlib.sha256(body).hexdigest() == result.data["sha256"]
                    outcomes.append(
                        {
                            "scenario": scenario,
                            "data_status": result.data_status,
                            "selected_method": result.data.get("selected_method"),
                            "page_reads": seen.count("/news"),
                        }
                    )
    print(
        json.dumps(
            {
                "ok": True,
                "network": "127.0.0.1 only",
                "browser_session": "injected fixture",
                "scenarios": outcomes,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
