"""The mutable object: the AIDE harness H_t (spec sections 3, 5, 10).

H_t is *only* data that the existing AIDE runner already knows how to consume:

  prompt_notes     text appended to the task notes.  AIDE stages notes into the
                   task description, so BOTH the code-generation prompts and (in
                   agent mode) the decision prompts read it.
  decision_policy  agent mode: extra decision-policy text (rendered under its own
                   heading in the same notes).
                   rule mode : a WHITELISTED rule configuration (bounded numbers).
  memory_policy    how many / how long the memory lessons are, and whether they are rendered into the
                   notes at all (`render`: none | lessons; see DEFAULT_MEMORY_POLICY).

  hooks/           optional harness-layer scripts: how memory is retrieved (`select_memory.py`) and how the
                   context reaching the agent is organised (`render_notes.py`).  They are code, but they are
                   harness responsibilities, so editing them is editing the harness (see hooks.py for the sandbox).

A harness version is a DIRECTORY of files (ALLOWED_FILES).  The improver agent edits a private copy of that
directory directly; the framework then checks the scope (only those files, nothing else) and that the result
runs, and stores the new version with a file-level diff as a post-hoc record.  Nothing else is reachable: no AIDE
source, no task data, no evaluator.

The older bounded-JSON-patch path (`apply_patch`) is kept as a legacy improver (`--improver patch`).
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .hooks import HOOKS, HookError, check_source, run_hook
from .schemas import MODES

# ------------------------------------------------------------------ whitelists
# name -> (min, max, type).  These are the only rule-mode knobs a patch may touch.
RULE_BOUNDS = {
    "max_stagnation": (0, 50, int),
    "debug_prob": (0.0, 1.0, float),
    "max_debug_depth": (0, 20, int),
    "num_drafts": (1, 10, int),
}
MEMORY_BOUNDS = {"max_records": (0, 50, int), "max_chars": (0, 20000, int)}
# `render` decides whether memory lessons are ALSO written into the agent's prompt notes:
#   "none"    (default for new harnesses) memory feeds only the meta-improver; the agent sees advice only
#             through patches, so an effect can be attributed to the patch and not to the auto-injected lesson
#   "lessons" memory lessons are appended to the notes automatically (what the first pilot did; a harness
#             saved without this key keeps that meaning, so already-committed versions are unchanged)
# `render` is fixed by the experiment arm and is NOT patchable by the meta-improver (not in MEMORY_BOUNDS).
DEFAULT_MEMORY_POLICY = {"max_records": 5, "max_chars": 1500, "render": "none"}
MEMORY_RENDER_MODES = ("none", "lessons")

# The harness boundary, by responsibility.  Anything outside this set is outside the harness.
ALLOWED_FILES = ("prompt_notes.md", "decision_policy.md", "rule_config.json", "memory_policy.json", "CHANGES.md",
                 "hooks/select_memory.py", "hooks/render_notes.py")
MAX_FILE_CHARS = 20_000
MAX_NOTES_CHARS = 20_000            # what the runner may receive as notes after rendering

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
    hooks: dict = field(default_factory=dict)   # {"hooks/render_notes.py": source, ...}
    changes: str = ""                       # free-text CHANGES.md written by the improver

    def __post_init__(self):
        if self.mode not in MODES:
            raise PatchError(f"unknown mode {self.mode!r}")
        unknown = set(self.hooks) - set(HOOKS)
        if unknown:
            raise PatchError(f"unknown hook file(s) {sorted(unknown)}")
        if self.memory_policy is None:
            self.memory_policy = dict(DEFAULT_MEMORY_POLICY)
        if self.memory_render not in MEMORY_RENDER_MODES:
            raise PatchError(f"memory_policy.render must be one of {MEMORY_RENDER_MODES}")
        if self.mode == "rule":
            self.rule_config = validate_rule_values(self.rule_config or {}, partial=True)
            self.decision_policy_text = ""
        else:
            self.rule_config = None

    @property
    def memory_render(self) -> str:
        # a harness stored before this key existed rendered lessons, so a missing key means "lessons"
        return (self.memory_policy or {}).get("render", "lessons")

    # ---- serialisation
    def to_dict(self) -> dict:
        d = {
            "task": self.task, "mode": self.mode, "version": self.version, "parent": self.parent,
            "prompt_notes": self.prompt_notes, "decision_policy_text": self.decision_policy_text,
            "rule_config": self.rule_config, "memory_policy": self.memory_policy,
        }
        if self.hooks:                       # only when present, so the hash of a hook-free harness is unchanged
            d["hooks"] = dict(self.hooks)
        if self.changes:
            d["changes"] = self.changes
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Harness":
        return cls(**{k: d.get(k) for k in ("task", "mode", "version", "parent", "prompt_notes",
                                             "decision_policy_text", "rule_config", "memory_policy",
                                             "hooks", "changes") if k in d})

    # ---- the file view the improver edits
    def to_files(self) -> dict[str, str]:
        files = {"prompt_notes.md": self.prompt_notes,
                 "memory_policy.json": json.dumps(self.memory_policy, indent=2, sort_keys=True) + "\n"}
        if self.mode == "agent":
            files["decision_policy.md"] = self.decision_policy_text
        else:
            files["rule_config.json"] = json.dumps(self.rule_config or {}, indent=2, sort_keys=True) + "\n"
        if self.changes:
            files["CHANGES.md"] = self.changes
        files.update(self.hooks)
        return files

    @classmethod
    def from_files(cls, files: dict[str, str], task: str, mode: str, version: str, parent: str | None) -> "Harness":
        """Parse an (already scope-checked) file view.  Raises PatchError on malformed JSON."""
        def load_json(name, default):
            if name not in files or not files[name].strip():
                return default
            try:
                value = json.loads(files[name])
            except json.JSONDecodeError as exc:
                raise PatchError(f"{name} is not valid JSON: {exc}")
            if not isinstance(value, dict):
                raise PatchError(f"{name} must contain a JSON object")
            return value
        memory_policy = load_json("memory_policy.json", None)
        rule_config = load_json("rule_config.json", {}) if mode == "rule" else None
        return cls(task=task, mode=mode, version=version, parent=parent,
                   prompt_notes=files.get("prompt_notes.md", ""),
                   decision_policy_text=files.get("decision_policy.md", "") if mode == "agent" else "",
                   rule_config=rule_config, memory_policy=memory_policy,
                   hooks={k: v for k, v in files.items() if k in HOOKS}, changes=files.get("CHANGES.md", ""))

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

    # ---- memory selection and rendering, honouring the harness-layer hooks
    def select_records(self, all_records: list[dict]) -> list[dict]:
        """Which memory records reach this harness's notes/rendering.  Default: worst reward first, one per
        distinct lesson, at most `max_records`.  `hooks/select_memory.py` replaces that rule."""
        from .memory import retrieve_from             # local import: memory.py does not depend on harness.py
        limit = int((self.memory_policy or {}).get("max_records", 5))
        source = self.hooks.get("hooks/select_memory.py")
        if source is None:
            return retrieve_from(all_records, limit)
        slim = [_slim_record(r) for r in all_records]
        picked = run_hook(source, "select_memory", [slim, self._hook_context(None)])
        if (not isinstance(picked, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in picked)
                or len(set(picked)) != len(picked) or any(not 0 <= i < len(all_records) for i in picked)):
            raise PatchError("select_memory must return a list of distinct valid record indices")
        return [all_records[i] for i in picked[:max(limit, 0)]]

    def _hook_context(self, round_):
        return {"mode": self.mode, "round": round_, "prompt_notes": self.prompt_notes,
                "decision_policy": self.decision_policy_text, "memory_policy": self.memory_policy,
                "max_chars": MAX_NOTES_CHARS}

    def render(self, memory_records=(), round_=None) -> str:
        """The exact notes text for a run.  `hooks/render_notes.py` (context organisation) replaces the default
        composition; without it, memory lessons are appended only if `memory_policy.render == "lessons"`.  Both
        paths end in the same gate: a string, at most MAX_NOTES_CHARS, no forbidden material."""
        from .memory import lessons_text
        source = self.hooks.get("hooks/render_notes.py")
        if source is None:
            mp = self.memory_policy or {}
            lessons = (lessons_text(list(memory_records), int(mp.get("max_chars", 1500)))
                       if self.memory_render == "lessons" else "")
            text = self.render_notes(lessons)
        else:
            ctx = {**self._hook_context(round_), "memory": [_slim_record(r) for r in memory_records]}
            text = run_hook(source, "render_notes", [ctx])
            if not isinstance(text, str):
                raise PatchError("render_notes must return a string")
        text = text.strip()
        if len(text) > MAX_NOTES_CHARS:
            raise PatchError(f"the rendered notes are {len(text)} characters (limit {MAX_NOTES_CHARS})")
        _check_text(text)
        return text


def _slim_record(rec: dict) -> dict:
    """Only what a hook may see of a memory record."""
    return {k: rec.get(k) for k in ("verifier", "reward", "lesson", "situation", "evidence", "decision_outcome",
                                    "round", "harness_version", "run_id")}


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


# ------------------------------------------------------- scope and runnability of a file view
@dataclass
class ScopeReport:
    ok: bool
    violations: list
    warnings: list
    changed: list
    rendered_sample: str | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "violations": self.violations, "warnings": self.warnings, "changed": self.changed,
                "rendered_sample_chars": None if self.rendered_sample is None else len(self.rendered_sample)}


_SAMPLE_RECORDS = [
    {"verifier": "validation_mirage", "reward": -0.12, "lesson": "sample lesson A", "situation": "sample",
     "evidence": "sample evidence", "decision_outcome": "sample", "round": 0, "harness_version": "H0", "run_id": "r1"},
    {"verifier": "submission_sanity", "reward": -1.0, "lesson": "sample lesson B", "situation": "sample",
     "evidence": "sample evidence", "decision_outcome": "sample", "round": 0, "harness_version": "H0", "run_id": "r2"},
]


def validate_files(files: dict[str, str], mode: str, parent_files: dict[str, str] | None = None,
                   sample_records: list[dict] | None = None, round_: int = 0) -> ScopeReport:
    """Is this file view (1) inside the harness boundary and (2) runnable?

    Boundary: only ALLOWED_FILES exist - nothing that names AIDE code, task data or the evaluator can be a
    file of the harness, and prompt text may not point at grader/answer material.  Runnability: JSON parses
    and is within bounds, hook scripts pass the static policy, and the harness renders notes.

    The render check uses what the version will ACTUALLY receive: `round_` (its own version number) and
    `sample_records` (the memory visible to it; None = only synthetic records).  It is rendered twice - two
    different results mean the harness is not reproducible - and once more on synthetic records, so a hook that
    only copes with an empty or a particular memory is caught.  `rendered_sample` is the full text of the real
    render; the caller persists it so that nobody has to re-execute a hook to obtain the notes again.
    """
    v: list[str] = []
    w: list[str] = []
    parent_files = parent_files or {}
    for path, text in files.items():
        if path not in ALLOWED_FILES:
            v.append(f"`{path}` is outside the harness boundary; the harness consists only of: "
                     + ", ".join(ALLOWED_FILES))
        elif len(text) > MAX_FILE_CHARS:
            v.append(f"`{path}` is {len(text)} characters (limit {MAX_FILE_CHARS})")
    for name in ("prompt_notes.md", "decision_policy.md"):
        low = files.get(name, "").lower()
        for bad in FORBIDDEN_TEXT:
            if bad in low:
                v.append(f"`{name}` mentions forbidden material {bad!r} (grader, answers or hidden data)")

    def parse(name):
        try:
            value = json.loads(files[name] or "{}")
        except json.JSONDecodeError as exc:
            v.append(f"`{name}` is not valid JSON: {exc}")
            return None
        if not isinstance(value, dict):
            v.append(f"`{name}` must contain a JSON object")
            return None
        return value

    if "rule_config.json" in files:
        cfg = parse("rule_config.json")
        if cfg is not None:
            try:
                validate_rule_values(cfg, partial=True)
            except PatchError as exc:
                v.append(f"`rule_config.json`: {exc}")
        if mode == "agent":
            w.append("`rule_config.json` is ignored in agent mode")
    if mode == "rule" and files.get("decision_policy.md", "").strip():
        w.append("`decision_policy.md` is ignored in rule mode (the rule policy does not read prompts)")
    if "memory_policy.json" in files:
        mp = parse("memory_policy.json")
        if mp is not None:
            extra = set(mp) - {"max_records", "max_chars", "render"}
            if extra:
                v.append(f"`memory_policy.json` has unknown key(s) {sorted(extra)}")
            try:
                _validate_memory_values({k: mp[k] for k in ("max_records", "max_chars") if k in mp})
            except PatchError as exc:
                v.append(f"`memory_policy.json`: {exc}")
            if "render" in mp and mp["render"] not in MEMORY_RENDER_MODES:
                v.append(f"`memory_policy.json`: render must be one of {MEMORY_RENDER_MODES}")
    for path, entry in HOOKS.items():
        if path in files:
            v.extend(f"`{path}`: {problem}" for problem in check_source(files[path], entry))
    changed = sorted(p for p in set(files) | set(parent_files) if files.get(p) != parent_files.get(p))
    if "memory_policy.json" in changed and parent_files:
        def _render(fs):
            try:
                return json.loads(fs.get("memory_policy.json") or "{}").get("render")
            except (json.JSONDecodeError, AttributeError):
                return None
        if _render(files) != _render(parent_files):
            w.append("`memory_policy.json` changes how memory reaches the agent's prompt: effects of this version "
                     "are confounded with the memory lessons it now receives")
    if v:
        return ScopeReport(False, v, w, changed)
    rendered = None
    try:
        h = Harness.from_files(files, "t", mode, "Hx", None)
        real = list(sample_records) if sample_records is not None else None
        probes = [(_SAMPLE_RECORDS, "synthetic memory")] + ([(real, "the real memory")] if real is not None else [])
        for records, label in probes:
            first = h.render(h.select_records(records), round_=round_)
            if h.hooks and h.render(h.select_records(records), round_=round_) != first:
                v.append(f"does not run reproducibly: two renders on {label} differ")
            rendered = first                                      # the last probe is the real one when there is one
    except (PatchError, HookError) as exc:
        v.append(f"does not run: {exc}")
    return ScopeReport(not v, v, w, changed, rendered)


# ----------------------------------------------------------------------- store
class HarnessStore:
    """harness_versions/<task>/<mode>/{H0,H1,...}/ + memory.jsonl (spec section 5).

    Each version directory holds `harness/` (the harness files), version.json (metadata) and, for t>0,
    diff.patch (file-level diff against the parent, a post-hoc record) plus patch.json (legacy patch improver) or
    improver_record.json (direct-edit improver), and rendered_notes.txt (the notes the version runs with, rendered once).
    Versions written by the first pilot hold a single harness.json
    instead; `load` reads both.  Every version is committed to Git and tagged `harness/<task>/<mode>/<version>`.
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
        vdir = self.version_dir(version)
        hdir = vdir / "harness"
        if hdir.is_dir():                                     # current format: a directory of harness files
            files = {str(p.relative_to(hdir)): p.read_text() for p in sorted(hdir.rglob("*")) if p.is_file()}
            parent = json.loads((vdir / "version.json").read_text()).get("parent")
            return Harness.from_files(files, self.task, self.mode, version, parent)
        return Harness.from_dict(json.loads((vdir / "harness.json").read_text()))   # legacy single-JSON version

    def files(self, version: str) -> dict[str, str]:
        return self.load(version).to_files()

    def create_initial(self, harness: Harness) -> Path:
        if harness.version != "H0" or harness.parent is not None:
            raise PatchError("initial harness must be version H0 with no parent")
        if self.versions():
            raise PatchError(f"{self.dir} already has versions {self.versions()}")
        return self._write(harness, patch=None, diff="")

    def next_version(self) -> str:
        latest = self.latest()
        return "H0" if latest is None else f"H{int(latest[1:]) + 1}"

    @staticmethod
    def diff_files(old: dict[str, str], new: dict[str, str], old_version: str, new_version: str) -> str:
        """File-level unified diff between two harness versions - a post-hoc record, not an edit format."""
        out = []
        for path in sorted(set(old) | set(new)):
            a, b = old.get(path), new.get(path)
            if a == b:
                continue
            chunk = list(difflib.unified_diff(
                (a or "").splitlines(True), (b or "").splitlines(True),
                fromfile=f"{old_version}/harness/{path}" if a is not None else "/dev/null",
                tofile=f"{new_version}/harness/{path}" if b is not None else "/dev/null"))
            if chunk and not chunk[-1].endswith("\n"):        # a file without trailing newline: keep the patch valid
                chunk[-1] += "\n\\ No newline at end of file\n"
            out.extend(chunk)
        return "".join(out)

    def commit_patch(self, parent: Harness, patch: dict) -> Harness:
        """Legacy path: validate + apply a bounded JSON patch + persist H_{t+1}.  Raises PatchError."""
        new = apply_patch(parent, patch, self.next_version())
        self._write(new, patch, self.diff_files(parent.to_files(), new.to_files(), parent.version, new.version))
        return new

    def commit_files(self, parent: Harness, files: dict[str, str], record: dict | None = None,
                     patch: dict | None = None, rendered: str | None = None) -> Harness:
        """Persist the improver's edited file view as H_{t+1}.  The caller has already run `validate_files`.
        `patch` is only given by the legacy patch improver, whose JSON patch is kept next to the files.
        `rendered` is the notes text the version will run with (rendered once, before the commit): it is stored
        and is what every later plan/collect/freeze/held-out step uses, so hooks are never re-executed to
        reproduce it."""
        new = Harness.from_files(files, self.task, self.mode, self.next_version(), parent.version)
        self._write(new, patch, self.diff_files(parent.to_files(), new.to_files(), parent.version, new.version), record,
                    rendered)
        return new

    def rendered(self, version: str) -> str | None:
        """The persisted notes of a version, or None for versions written before notes were persisted (and H0)."""
        path = self.version_dir(version) / "rendered_notes.txt"
        return path.read_text() if path.is_file() else None

    def _write(self, harness: Harness, patch: dict | None, diff: str, record: dict | None = None,
               rendered: str | None = None) -> Path:
        vdir = self.version_dir(harness.version)
        vdir.mkdir(parents=True, exist_ok=False)
        for path, text in harness.to_files().items():
            target = vdir / "harness" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        meta = {"version": harness.version, "parent": harness.parent, "sha256": harness.sha256(),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if patch is not None:
            (vdir / "patch.json").write_text(json.dumps(patch, indent=2, ensure_ascii=False) + "\n")
            meta["patch_sha256"] = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
        if harness.parent is not None:
            (vdir / "diff.patch").write_text(diff)
        if rendered is not None:
            (vdir / "rendered_notes.txt").write_text(rendered)
            meta["rendered_notes_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
        if record is not None:
            (vdir / "improver_record.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n")
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
