"""Compact experience memory carried across RSI rounds (spec section 9).

One record per important episode (never a transcript):

    situation | decision_outcome | verifier | reward | evidence | lesson

A record is written for each verifier that fired (reward < 0), plus one
"clean" record when nothing fired so the meta-improver can see what worked.
Lessons come from a fixed table keyed by verifier: they are deterministic on
purpose, so a memory record is reproducible from a verifier result and never
depends on an LLM having been in a good mood.
"""
from __future__ import annotations

import json
from pathlib import Path

from .schemas import make_memory_record, validate_memory_record
from .trajectory import Trajectory

LESSONS = {
    "validation_argmax_anchoring":
        "Do not follow the highest validation score alone; before choosing what to refine or submit, "
        "list evidence that contradicts it (implausibly high score, leakage, unstable folds) and prefer a "
        "candidate whose validation is trustworthy.",
    "submission_sanity":
        "Before finishing a candidate, print the prediction distribution (range, spread, NaNs, row count) "
        "and check it against the required submission format; a constant or out-of-range prediction is a bug.",
    "validation_mirage":
        "Fit every preprocessing/resampling step inside the training fold only, never use fields that do "
        "not exist at prediction time, and be suspicious of a validation score that looks too good.",
    "extraction_distortion":
        "Report the validation metric exactly as printed by the run (mean over folds, not the best fold); "
        "a number that never appears in the output must not be recorded.",
    "simplified_error_attribution":
        "When validation and test-like behaviour disagree, check direct causes (misalignment, leakage, "
        "scale) before attributing the failure to generic overfitting.",
    "fallback_to_runnable":
        "Do not abandon a promising approach just because it errored; diagnose and fix the failure before "
        "falling back to a simpler model.",
}
CLEAN_LESSON = "No verifier fired for this run; keep the current behaviour."


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(validate_memory_record(json.loads(line)))
        return out

    def append(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            for rec in records:
                f.write(json.dumps(validate_memory_record(rec), ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ write
    def records_from_run(self, traj: Trajectory, bank: dict, round_: int) -> list[dict]:
        final = next((e for e in traj.events if e["decision_type"] == "FINAL_SELECTION"), None)
        situation = final["state_summary"] if final else f"{len(traj.nodes)} nodes; no final selection recorded"
        outcome = final["action"] if final else "no final selection"
        if traj.grade_revealed and traj.grade and traj.grade.get("score") is not None:
            outcome += f" -> official score {traj.grade['score']}"
        prov = {"task": traj.task, "mode": traj.mode, "round": round_,
                "harness_version": traj.harness_version, "run_id": traj.run_dir.name}
        recs = []
        for res in bank["results"]:
            if res["status"] != "ok" or res["reward"] >= 0:
                continue
            recs.append(make_memory_record(
                situation, outcome, res["verifier"], res["reward"],
                (res["evidence"][0] if res["evidence"] else res["explanation"])[:400],
                LESSONS.get(res["verifier"], res["explanation"][:200]), **prov))
        if not recs:
            recs.append(make_memory_record(situation, outcome, "none", 0.0,
                                           "all enabled verifiers returned reward >= 0", CLEAN_LESSON, **prov))
        return recs

    # ------------------------------------------------------------------- read
    def retrieve(self, max_records: int) -> list[dict]:
        """Worst reward first, newest first among ties; one record per distinct
        lesson so a repeated failure does not crowd out other lessons."""
        seen, out = set(), []
        ranked = sorted(enumerate(self.all()), key=lambda t: (t[1]["reward"], -t[0]))
        for _, rec in ranked:
            if rec["verifier"] == "none" or rec["lesson"] in seen:
                continue
            seen.add(rec["lesson"])
            out.append(rec)
            if len(out) >= max_records:
                break
        return out

    def render_lessons(self, max_records: int, max_chars: int) -> str:
        lines = []
        for rec in self.retrieve(max_records):
            lines.append(f"- {rec['lesson']}")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]
