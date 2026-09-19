"""The mutable object: the AIDE harness H_t (spec sections 3, 5, 10).

H_t is *only* data that the existing AIDE runner already knows how to consume:

  prompt_notes     text appended to the task notes.  AIDE stages notes into the
                   task description, so BOTH the code-generation prompts and (in
                   agent mode) the decision prompts read it.
  decision_policy  agent mode: extra decision-policy text (rendered under its own
                   heading in the same notes).
                   rule mode : a WHITELISTED rule configuration (bounded numbers).
  memory_policy    how many / how long the memory lessons rendered into the notes.

Nothing else is reachable: no AIDE source, no task environment, no evaluator.
A patch is validated, bounded and stored with its exact diff; an invalid patch
never produces a new version.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schemas import MODES

# ------------------------------------------------------------------ whitelists
# name -> (min, max, type).  These are the only rule-mode knobs a patch may touch.
RULE_BOUNDS = {
    "max_stagnation": (0, 50, int),
    "debug_prob": (0.0, 1.0, float),
    "max_debug_depth": (0, 20, int),
    "num_drafts": (1, 10, int),
}
MEMORY_BOUNDS = {"max_records": (0, 10, int), "max_chars": (0, 3000, int)}
DEFAULT_MEMORY_POLICY = {"max_records": 5, "max_chars": 1500}

TARGETS = ("prompt", "decision_policy", "memory_policy")
MAX_APPEND_CHARS = 800
MAX_TEXT_CHARS = 4000
MIN_REPLACE_SIMILARITY = 0.6
MAX_RULE_KEYS_PER_PATCH = 2
# Integrity guard: a patch must not point the agent at grader/answer material.
FORBIDDEN_TEXT = ("/private", "answers.csv", "solution.csv", "test labels", "hidden test", "grade_report")


class PatchError(ValueError):
    pass


@dataclass
class Harness:
    task: str
    mode: str
    version: str
    parent: str | None
    prompt_notes: str = ""
    decision_policy_text: str = ""          # agent mode only
    rule_config: dict | None = None         # rule mode only
    memory_policy: dict | None = None

    def __post_init__(self):
        if self.mode not in MODES:
            raise PatchError(f"unknown mode {self.mode!r}")
        if self.memory_policy is None:
            self.memory_policy = dict(DEFAULT_MEMORY_POLICY)
        if self.mode == "rule":
            self.rule_config = validate_rule_values(self.rule_config or {}, partial=True)
            self.decision_policy_text = ""
        else:
            self.rule_config = None

    # ---- serialisation
    def to_dict(self) -> dict:
        return {
            "task": self.task, "mode": self.mode, "version": self.version, "parent": self.parent,
            "prompt_notes": self.prompt_notes, "decision_policy_text": self.decision_policy_text,
            "rule_config": self.rule_config, "memory_policy": self.memory_policy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Harness":
        return cls(**{k: d.get(k) for k in ("task", "mode", "version", "parent", "prompt_notes",
                                             "decision_policy_text", "rule_config", "memory_policy")
                      if k in d})

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    # ---- what the runner receives
    def render_notes(self, memory_lessons: str = "") -> str:
        """Exact text appended to the task notes.  Empty for the stock H0."""
        parts = []
        if self.prompt_notes.strip():
            parts.append(self.prompt_notes.strip())
        if self.mode == "agent" and self.decision_policy_text.strip():
            parts.append("## Decision policy (for choosing what to try next and what to submit)\n"
                         + self.decision_policy_text.strip())
        if memory_lessons.strip():
            parts.append("## Lessons from earlier runs on similar tasks\n" + memory_lessons.strip())
        return "\n\n".join(parts)


# ------------------------------------------------------------------- validation
def validate_rule_values(values: dict, partial: bool) -> dict:
    out = {}
    for key, val in values.items():
        if key not in RULE_BOUNDS:
            raise PatchError(f"rule key {key!r} is not whitelisted; allowed: {sorted(RULE_BOUNDS)}")
        lo, hi, typ = RULE_BOUNDS[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise PatchError(f"rule.{key} must be a number")
        if typ is int and float(val) != int(val):
            raise PatchError(f"rule.{key} must be an integer")
        val = typ(val)
        if not lo <= val <= hi:
            raise PatchError(f"rule.{key}={val} outside [{lo}, {hi}]")
        out[key] = val
    return out


def _validate_memory_values(values: dict) -> dict:
    out = {}
    for key, val in values.items():
        if key not in MEMORY_BOUNDS:
            raise PatchError(f"memory_policy key {key!r} not allowed; allowed: {sorted(MEMORY_BOUNDS)}")
        lo, hi, _ = MEMORY_BOUNDS[key]
        if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
            raise PatchError(f"memory_policy.{key} must be an int in [{lo}, {hi}]")
        out[key] = val
    return out


def _check_text(text: str) -> None:
    low = text.lower()
    for bad in FORBIDDEN_TEXT:
        if bad in low:
            raise PatchError(f"patch text mentions forbidden material {bad!r}")


def validate_patch(patch: dict) -> dict:
    """Shape check of the meta-improver output (spec section 10)."""
    if not isinstance(patch, dict):
        raise PatchError("patch must be a JSON object")
    for key in ("target_component", "proposed_patch", "expected_effect", "evidence"):
        if key not in patch:
            raise PatchError(f"patch missing '{key}'")
    if patch["target_component"] not in TARGETS:
        raise PatchError(f"target_component must be one of {TARGETS}")
    if not isinstance(patch["proposed_patch"], dict) or "op" not in patch["proposed_patch"]:
        raise PatchError("proposed_patch must be an object with an 'op'")
    if not isinstance(patch["expected_effect"], str) or not patch["expected_effect"].strip():
        raise PatchError("expected_effect must be a non-empty string")
    if (not isinstance(patch["evidence"], list) or not patch["evidence"]
            or not all(isinstance(e, str) and e.strip() for e in patch["evidence"])):
        raise PatchError("evidence must be a non-empty list of strings")
    return patch


def _edit_text(current: str, proposed: dict) -> str:
    op, text = proposed["op"], proposed.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PatchError("text patch needs a non-empty 'text'")
    _check_text(text)
    if op == "append":
        if len(text) > MAX_APPEND_CHARS:
            raise PatchError(f"append longer than {MAX_APPEND_CHARS} chars")
        if text.strip() in current:
            raise PatchError("append text already present (no-op patch)")
        new = (current.rstrip() + "\n" + text.strip()).strip()
    elif op == "replace":
        ratio = difflib.SequenceMatcher(None, current, text).ratio()
        if current and ratio < MIN_REPLACE_SIMILARITY:
            raise PatchError(f"replace too large: similarity {ratio:.2f} < {MIN_REPLACE_SIMILARITY}")
        new = text.strip()
    else:
        raise PatchError(f"unsupported text op {op!r}; use 'append' or 'replace'")
    if len(new) > MAX_TEXT_CHARS:
        raise PatchError(f"resulting text longer than {MAX_TEXT_CHARS} chars")
    return new


def apply_patch(harness: Harness, patch: dict, new_version: str) -> Harness:
    """Pure function: H_t + validated patch -> H_{t+1}.  Raises PatchError."""
    validate_patch(patch)
    target, proposed = patch["target_component"], patch["proposed_patch"]
    new = copy.deepcopy(harness)
    new.version, new.parent = new_version, harness.version

    if target == "prompt":
        new.prompt_notes = _edit_text(harness.prompt_notes, proposed)
    elif target == "decision_policy" and harness.mode == "agent":
        new.decision_policy_text = _edit_text(harness.decision_policy_text, proposed)
    elif target == "decision_policy" and harness.mode == "rule":
        if proposed["op"] != "set" or not isinstance(proposed.get("values"), dict):
            raise PatchError("rule decision_policy patch must be {'op':'set','values':{...}}")
        values = validate_rule_values(proposed["values"], partial=True)
        if not values or len(values) > MAX_RULE_KEYS_PER_PATCH:
            raise PatchError(f"rule patch must change 1..{MAX_RULE_KEYS_PER_PATCH} keys")
        merged = {**(harness.rule_config or {}), **values}
        if merged == (harness.rule_config or {}):
            raise PatchError("rule patch changes nothing (no-op patch)")
        new.rule_config = merged
    elif target == "memory_policy":
        if proposed["op"] != "set" or not isinstance(proposed.get("values"), dict):
            raise PatchError("memory_policy patch must be {'op':'set','values':{...}}")
        values = _validate_memory_values(proposed["values"])
        merged = {**harness.memory_policy, **values}
        if not values or merged == harness.memory_policy:
            raise PatchError("memory_policy patch changes nothing (no-op patch)")
        new.memory_policy = merged
    return new


# ----------------------------------------------------------------------- store
class HarnessStore:
    """harness_versions/<task>/<mode>/{H0,H1,...}/ + memory.jsonl (spec section 5).

    Each version directory holds harness.json, version.json (metadata), and for
    t>0 patch.json + diff.patch (the exact diff against the parent).  Every
    version is committed to Git and tagged `harness/<task>/<mode>/<version>`.
    """

    def __init__(self, root: str | Path, task: str, mode: str):
        self.root = Path(root)
        self.task, self.mode = task, mode
        self.dir = self.root / task / mode

    @property
    def memory_path(self) -> Path:
        return self.dir / "memory.jsonl"

    def version_dir(self, version: str) -> Path:
        return self.dir / version

    def versions(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        found = [p.name for p in self.dir.iterdir() if p.is_dir() and p.name.startswith("H")]
        return sorted(found, key=lambda v: int(v[1:]))

    def latest(self) -> str | None:
        vs = self.versions()
        return vs[-1] if vs else None

    def load(self, version: str) -> Harness:
        return Harness.from_dict(json.loads((self.version_dir(version) / "harness.json").read_text()))

    def create_initial(self, harness: Harness) -> Path:
        if harness.version != "H0" or harness.parent is not None:
            raise PatchError("initial harness must be version H0 with no parent")
        if self.versions():
            raise PatchError(f"{self.dir} already has versions {self.versions()}")
        return self._write(harness, patch=None, diff="")

    def next_version(self) -> str:
        latest = self.latest()
        return "H0" if latest is None else f"H{int(latest[1:]) + 1}"

    def commit_patch(self, parent: Harness, patch: dict) -> Harness:
        """Validate + apply + persist H_{t+1}; returns it.  Raises PatchError."""
        new = apply_patch(parent, patch, self.next_version())
        old_text = json.dumps(parent.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        new_text = json.dumps(new.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(True), new_text.splitlines(True),
            fromfile=f"{parent.version}/harness.json", tofile=f"{new.version}/harness.json"))
        self._write(new, patch, diff)
        return new

    def _write(self, harness: Harness, patch: dict | None, diff: str) -> Path:
        vdir = self.version_dir(harness.version)
        vdir.mkdir(parents=True, exist_ok=False)
        (vdir / "harness.json").write_text(
            json.dumps(harness.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        meta = {"version": harness.version, "parent": harness.parent, "sha256": harness.sha256(),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if patch is not None:
            (vdir / "patch.json").write_text(json.dumps(patch, indent=2, ensure_ascii=False) + "\n")
            (vdir / "diff.patch").write_text(diff)
            meta["patch_sha256"] = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
        (vdir / "version.json").write_text(json.dumps(meta, indent=2) + "\n")
        self._git_commit(vdir, harness)
        return vdir

    def _git_commit(self, vdir: Path, harness: Harness) -> None:
        """Best effort: commit + tag the version inside this repo.  A missing git
        or a non-repo directory downgrades to a warning file, never a crash."""
        tag = f"harness/{self.task}/{self.mode}/{harness.version}"
        try:
            top = subprocess.run(["git", "-C", str(vdir), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "-C", top, "add", str(vdir)], check=True, capture_output=True)
            subprocess.run(["git", "-C", top, "commit", "-q", "-m", f"harness {tag}", "--", str(vdir)],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", top, "tag", "-f", tag], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            (vdir / "NOT_COMMITTED.txt").write_text(f"git commit/tag skipped: {exc}\n")
