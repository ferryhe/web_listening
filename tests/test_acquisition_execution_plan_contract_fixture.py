import json
from pathlib import Path


def test_acquisition_execution_plan_fixture_is_compact_canonical_json():
    path = Path(__file__).parents[1] / "docs/testing/fixtures/acquisition-execution-plan-v1.sample.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "acquisition-execution-plan-preview.v1"
    assert payload["plan"]["schema_version"] == "acquisition-execution-plan.v1"
    assert path.read_text(encoding="utf-8").strip() == json.dumps(payload, sort_keys=True, separators=(",", ":"))
