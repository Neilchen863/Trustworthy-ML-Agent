import hashlib
import re
from pathlib import Path

import pytest

from rsi_mvp import schemas

ROOT = Path(__file__).resolve().parents[1]


def test_vendored_scanner_matches_recorded_sha256():
    text = (ROOT / "vendor/issue_scanner/VENDORED.md").read_text()
    recorded = re.search(r"sha256\s*:\s*([0-9a-f]{64})", text).group(1)
    actual = hashlib.sha256((ROOT / "vendor/issue_scanner/audit_run.py").read_bytes()).hexdigest()
    assert actual == recorded, "vendored scanner was edited; refresh VENDORED.md deliberately"


def test_event_schema_accepts_spec_example():
    ev = schemas.make_event(12, "best val=0.812; latest candidate failed", "FAILURE_DIAGNOSIS",
                            "fallback_to_previous_model", "...", ["run_11.log", "metrics_11.json"], None)
    assert ev["outcome"] is None and ev["evidence"] == ["run_11.log", "metrics_11.json"]


@pytest.mark.parametrize("bad", [
    dict(step="1"), dict(decision_type="NOPE"), dict(action=""), dict(evidence="x"), dict(rationale=3)])
def test_validate_event_rejects_malformed(bad):
    ev = dict(step=1, state_summary="s", decision_type="FALLBACK", action="a", rationale=None, evidence=[],
              outcome=None)
    ev.update(bad)
    with pytest.raises(schemas.SchemaError):
        schemas.validate_event(ev)


def test_make_event_coerces_step_but_rejects_bare_string_evidence():
    assert schemas.make_event("7", "s", "FALLBACK", "a")["step"] == 7   # journal steps are strings
    with pytest.raises(schemas.SchemaError, match="bare str"):
        schemas.make_event(1, "s", "FALLBACK", "a", evidence="run_11.log")  # would silently become chars


def test_all_nine_spec_decision_types_exist():
    assert set(schemas.DECISION_TYPES) == {
        "EXPERIMENT_PROPOSAL", "EXPERIMENT_LAUNCH", "RESULT_INTERPRETATION", "FAILURE_DIAGNOSIS",
        "CANDIDATE_SELECTION", "FALLBACK", "MEMORY_WRITE", "FINAL_SELECTION", "SUBMISSION"}


def test_verifier_result_and_memory_record_schemas():
    r = schemas.make_verifier_result("v", [31, 31], -1.0, ["e"], "why")
    assert r["decision_steps"] == [31]
    with pytest.raises(schemas.SchemaError):
        schemas.validate_verifier_result({"verifier": "v"})
    m = schemas.make_memory_record("s", "o", "v", -1, "e", "l", task="t")
    assert m["task"] == "t" and set(schemas.MEMORY_FIELDS) <= set(m)
    with pytest.raises(schemas.SchemaError):
        schemas.validate_memory_record({"situation": "x"})
