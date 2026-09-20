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
import re
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
            scanner = load_scanner()
            _ctx, self.findings = scanner.run_audit(self.run_dir)
            self.n_nodes = len(_ctx.get("nodes", []))
            # The scanner counts pattern hits over every node that has code (buggy ones included), so
            # that is the denominator of a node-level rate.
            self.n_working = sum(1 for n in _ctx.get("nodes", []) if (n.get("code") or "").strip())
        except Exception as exc:  # a scanner crash must surface as status=error, not reward 0
            self.error = f"{type(exc).__name__}: {exc}"
            self.n_nodes = 0
            self.n_working = 0
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


_HITS = re.compile(r"\[共 (\d+) 个节点命中此模式\]")


def _hit_count(finding: dict) -> int:
    """How many nodes share this finding's pattern.  The scanner keeps ONE exemplar finding per pattern
    per run and appends '[共 N 个节点命中此模式]' to its message only when N > 1 (so no suffix means 1)."""
    m = _HITS.search(finding.get("message", ""))
    return int(m.group(1)) if m else 1


def _evidence(finding: dict) -> str:
    text = f"[{finding['layer']}/{finding['detector']}/{finding['severity']}] {finding['message']}"
    if finding.get("evidence") is not None:
        text += " | " + json.dumps(finding["evidence"], ensure_ascii=False, default=str)[:240]
    return text[:520]


def verify(verifier: str, scan: IssueScannerRun, reward_map: dict[str, float],
           min_severity: str = "warn", overrides: dict | None = None,
           units: dict | None = None, actionable_rate: float = 0.01) -> dict:
    """Result for one verifier from a shared scan.

    unit "run"  (default): reward = severity of the worst finding, as before.
    unit "node": reward = -(nodes flagged / nodes with code), so 1/445 and 30/445 are told apart.  Only
                 the M-layer names its nodes and reports how many share a pattern (one exemplar per pattern,
                 count in the message); the E-layer and aggregate S-layer findings truncate their id lists, so
                 those verifiers stay run-level.  Patterns can overlap and the union is not recoverable, so
                 `rate` is a LOWER bound (largest single pattern) and `rate_upper` the sum, capped at 1.  A
                 finding that names no node (an aggregate failure) still counts through its severity and can
                 never be diluted to zero by the node fraction.
    """
    ov = (overrides or {}).get(verifier, {})
    floor = ov.get("min_severity", min_severity)
    rmap = {**reward_map, **{k: float(v) for k, v in ov.get("reward_map", {}).items()}}
    unit = (units or {}).get(verifier, "run")
    if scan.error:
        return make_verifier_result(verifier, [], 0.0, [scan.error],
                                    "issue scanner crashed; no reward assigned", status="error",
                                    unit=unit, rate=None, actionable=False)
    if scan.n_nodes == 0:
        return make_verifier_result(verifier, [], 0.0, [], "run has no journal nodes", status="not_applicable",
                                    unit=unit, rate=None, actionable=False)
    route = ROUTES[verifier]
    # The scanner skips its whole submission layer when pandas is missing and says so only with an
    # info-level B_skip.  That is "could not check", not "checked and clean": never award reward 0.
    if verifier == "submission_sanity" and any(f["detector"] == "B_skip" for f in scan.findings):
        return make_verifier_result(verifier, [], 0.0, [], "scanner skipped submission checks: pandas is not "
                                    "installed in this interpreter", status="not_applicable",
                                    unit=unit, rate=None, actionable=False)
    mine = [f for f in scan.findings if _routed_to(f) == verifier
            and _SEV_ORDER[f["severity"]] >= _SEV_ORDER[floor]]
    n_units = scan.n_working if unit == "node" else 1
    if not mine:
        return make_verifier_result(verifier, [], rmap.get("clean", 0.0), [],
                                    "no finding at or above the severity floor",
                                    unit=unit, rate=0.0 if unit == "node" else None, n_flagged=0,
                                    n_units=n_units, actionable=False, severity_reward=rmap.get("clean", 0.0))
    worst = max(mine, key=lambda f: _SEV_ORDER[f["severity"]])["severity"]
    sev_reward = rmap.get(worst, rmap.get("fail", -1.0))
    evidence = [_evidence(f) for f in mine]
    evidence += [_evidence(f) for f in scan.findings if f["detector"] in route.get("context", ())]
    detectors = sorted({f["detector"] for f in mine})
    steps = scan.steps_for(mine)

    if unit == "node" and scan.n_working > 0:
        attributed = [f for f in mine if f.get("node")]
        unattributed = [f for f in mine if not f.get("node")]
        hits = [_hit_count(f) for f in attributed]
        n_flagged = min(max(hits, default=0), scan.n_working)                 # lower bound of the union
        rate = n_flagged / scan.n_working
        rate_upper = min(1.0, sum(hits) / scan.n_working)                       # patterns may overlap
        reward = -rate if attributed else 0.0
        unattributed_rewards = [rmap.get(f["severity"], sev_reward) for f in unattributed]
        if unattributed_rewards:               # aggregate failure: keep its severity, never dilute it
            reward = min(reward, min(unattributed_rewards))
        actionable = rate >= actionable_rate or any(x < 0 for x in unattributed_rewards)
        explanation = (f"{n_flagged}/{scan.n_working} nodes flagged by the largest pattern ({rate:.1%}; at most "
                       f"{rate_upper:.1%} if the {len(attributed)} pattern(s) do not overlap) from "
                       f"{', '.join(detectors)}"
                       + (f"; plus {len(unattributed)} aggregate finding(s) with no node" if unattributed else "")
                       + ". " + mine[0]["message"])[:600]
        return make_verifier_result(verifier, steps, reward, evidence, explanation, unit="node", rate=rate,
                                    rate_upper=rate_upper, n_flagged=n_flagged, n_units=scan.n_working,
                                    actionable=bool(actionable), severity_reward=sev_reward)

    explanation = (f"{len(mine)} finding(s) from {', '.join(detectors)}; worst severity {worst}. "
                   + mine[0]["message"])[:600]
    return make_verifier_result(verifier, steps, sev_reward, evidence, explanation, unit="run", rate=None,
                                n_flagged=1, n_units=1, actionable=sev_reward < 0, severity_reward=sev_reward)


IMPLEMENTED = tuple(ROUTES)
