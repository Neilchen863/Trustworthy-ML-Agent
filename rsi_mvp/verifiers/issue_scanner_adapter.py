"""Wrap the existing issue scanner (vendor/issue_scanner/audit_run.py) behind the
common verifier interface (spec section 7).  The scanner's logic is reused
untouched; this module only

  1. runs it once per trajectory,
  2. routes each finding (by detector id) to the verifier it evidences,
  3. converts severity -> reward and node -> decision step.

Findings the scanner emits but no verifier claims are counted in
`unmapped_detectors` so a scanner upgrade that adds detectors is visible rather
than silently ignored.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ..schemas import make_verifier_result

_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "issue_scanner" / "audit_run.py"
_module = None


def _restore_parents(load_run):
    """Input normalisation, not a logic change.  Real AIDE journals keep parent links only in
    the top-level `node2parent` map (every node's own `parent` is empty), but the scanner walks
    `node["parent"]`.  Without this it reports e.g. "descendants: 0 (0% of budget on the
    contaminated lineage)" for a run where 97% of nodes descend from phantom nodes, and that
    wrong evidence would be handed to the meta-improver.  Detector ids/severities are unchanged."""
    def wrapped(run_dir, strict_journal=False):
        ctx = load_run(run_dir, strict_journal)
        journal = Path(run_dir) / "logs" / "journal.json"
        try:
            node2parent = json.loads(journal.read_text()).get("node2parent", {}) if journal.is_file() else {}
        except (json.JSONDecodeError, AttributeError):
            node2parent = {}
        for node in ctx["nodes"]:
            if not node.get("parent") and node.get("id") in node2parent:
                node["parent"] = node2parent[node["id"]]
        return ctx
    return wrapped


def load_scanner():
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location("rsi_vendor_audit_run", _VENDOR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.load_run = _restore_parents(mod.load_run)
        _module = mod
    return _module


# verifier -> detector ids that evidence it.  Layer prefixes ("M*") match any id
# whose layer letter is that prefix (all M-layer measurement detectors).
ROUTES: dict[str, dict] = {
    "validation_argmax_anchoring": {
        "detectors": {"S1_submitted_phantom", "S1_phantom_nodes", "S4_selection_regret", "S7_val_beats_gold"},
        # info-level facts attached as evidence only when the verifier fired
        "context": {"S2_submit_val_rank"},
    },
    "submission_sanity": {"layers": {"B"}, "detectors": set(), "ignore": {"B_ok", "B_skip"}},
    "validation_mirage": {"layers": {"M"}, "detectors": {"S3_val_test_chasm", "S8_tune_on_invalid_val"}},
    "extraction_distortion": {
        "detectors": {"E1_metric_not_in_output", "E1v_verbose_fabrication", "E2_best_fold_pick",
                      "E4_nan_val_recorded_numeric", "E5_metric_identity_mix", "E5_metric_direction_wrong"},
    },
    "simplified_error_attribution": {"detectors": {"E3_chasm_misattribution"}},
    "fallback_to_runnable": {"detectors": {"S6_nn_abandoned"}},
}

# Detectors deliberately not routed to any verifier: they describe coverage of
# the scanner itself or run health, not a decision the harness made.
NOT_DECISIONS_LAYERS = {"D"}
NOT_DECISIONS_IDS = {"E0_coverage_gap"}

_SEV_ORDER = {"info": 0, "warn": 1, "fail": 2}


def _routed_to(finding: dict) -> str | None:
    det, layer = finding["detector"], finding["layer"]
    if det in NOT_DECISIONS_IDS or layer in NOT_DECISIONS_LAYERS:
        return None
    for verifier, route in ROUTES.items():
        if det in route.get("ignore", ()):
            continue
        if det in route.get("detectors", ()) or layer in route.get("layers", ()):
            return verifier
    return None


class IssueScannerRun:
    """One scan of one run directory, shared by every wrapped verifier."""

    def __init__(self, run_dir: Path, node_step: dict[str, int], final_step: int | None):
        self.run_dir = Path(run_dir)
        self.node_step = node_step
        self.final_step = final_step
        self.error: str | None = None
        self.findings: list[dict] = []
        try:
            _ctx, self.findings = load_scanner().run_audit(self.run_dir)
            self.n_nodes = len(_ctx.get("nodes", []))
        except Exception as exc:  # a scanner crash must surface as status=error, not reward 0
            self.error = f"{type(exc).__name__}: {exc}"
            self.n_nodes = 0
        self.unmapped = sorted({f["detector"] for f in self.findings
                                if f["severity"] != "info" and _routed_to(f) is None
                                and f["layer"] not in NOT_DECISIONS_LAYERS
                                and f["detector"] not in NOT_DECISIONS_IDS})

    def steps_for(self, findings: list[dict]) -> list[int]:
        steps = []
        for f in findings:
            node = f.get("node")
            if node and node in self.node_step:
                steps.append(self.node_step[node])
            elif self.final_step is not None:
                steps.append(self.final_step)
        return steps


def _evidence(finding: dict) -> str:
    text = f"[{finding['layer']}/{finding['detector']}/{finding['severity']}] {finding['message']}"
    if finding.get("evidence") is not None:
        text += " | " + json.dumps(finding["evidence"], ensure_ascii=False, default=str)[:240]
    return text[:520]


def verify(verifier: str, scan: IssueScannerRun, reward_map: dict[str, float],
           min_severity: str = "warn", overrides: dict | None = None) -> dict:
    """Result for one verifier from a shared scan."""
    ov = (overrides or {}).get(verifier, {})
    floor = ov.get("min_severity", min_severity)
    rmap = {**reward_map, **{k: float(v) for k, v in ov.get("reward_map", {}).items()}}
    if scan.error:
        return make_verifier_result(verifier, [], 0.0, [scan.error],
                                    "issue scanner crashed; no reward assigned", status="error")
    if scan.n_nodes == 0:
        return make_verifier_result(verifier, [], 0.0, [], "run has no journal nodes", status="not_applicable")
    route = ROUTES[verifier]
    mine = [f for f in scan.findings if _routed_to(f) == verifier
            and _SEV_ORDER[f["severity"]] >= _SEV_ORDER[floor]]
    if not mine:
        return make_verifier_result(verifier, [], rmap.get("clean", 0.0), [],
                                    "no finding at or above the severity floor")
    worst = max(mine, key=lambda f: _SEV_ORDER[f["severity"]])["severity"]
    reward = rmap.get(worst, rmap.get("fail", -1.0))
    evidence = [_evidence(f) for f in mine]
    evidence += [_evidence(f) for f in scan.findings if f["detector"] in route.get("context", ())]
    detectors = sorted({f["detector"] for f in mine})
    explanation = (f"{len(mine)} finding(s) from {', '.join(detectors)}; worst severity {worst}. "
                   + mine[0]["message"])[:600]
    return make_verifier_result(verifier, scan.steps_for(mine), reward, evidence, explanation)


IMPLEMENTED = tuple(ROUTES)
