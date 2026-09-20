import json
import subprocess

import pytest

from rsi_mvp.harness import (MAX_APPEND_CHARS, Harness, HarnessStore, PatchError, apply_patch, validate_patch)


def patch(target, proposed, effect="fix it", evidence=("e",)):
    return {"target_component": target, "proposed_patch": proposed, "expected_effect": effect,
            "evidence": list(evidence)}


@pytest.fixture
def agent_h():
    return Harness(task="t", mode="agent", version="H0", parent=None)


@pytest.fixture
def rule_h():
    return Harness(task="t", mode="rule", version="H0", parent=None,
                   rule_config={"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5})


def test_prompt_append_and_replace(agent_h):
    h1 = apply_patch(agent_h, patch("prompt", {"op": "append", "text": "Check the submission format."}), "H1")
    assert h1.prompt_notes == "Check the submission format." and h1.parent == "H0" and h1.version == "H1"
    h2 = apply_patch(h1, patch("prompt", {"op": "replace", "text": "Check the submission format carefully."}), "H2")
    assert "carefully" in h2.prompt_notes
    with pytest.raises(PatchError, match="too large"):
        apply_patch(h1, patch("prompt", {"op": "replace", "text": "Completely different unrelated words " * 3}), "H2")


def test_patch_is_bounded(agent_h):
    with pytest.raises(PatchError, match="longer"):
        apply_patch(agent_h, patch("prompt", {"op": "append", "text": "x" * (MAX_APPEND_CHARS + 1)}), "H1")
    h1 = apply_patch(agent_h, patch("prompt", {"op": "append", "text": "same"}), "H1")
    with pytest.raises(PatchError, match="no-op"):
        apply_patch(h1, patch("prompt", {"op": "append", "text": "same"}), "H2")


@pytest.mark.parametrize("text", ["read /private/data first", "look at answers.csv", "use the hidden test set"])
def test_integrity_guard_rejects_grader_material(agent_h, text):
    with pytest.raises(PatchError, match="forbidden"):
        apply_patch(agent_h, patch("prompt", {"op": "append", "text": text}), "H1")


def test_rule_patch_whitelist_and_bounds(rule_h):
    h1 = apply_patch(rule_h, patch("decision_policy", {"op": "set", "values": {"max_stagnation": 8}}), "H1")
    assert h1.rule_config["max_stagnation"] == 8 and h1.rule_config["debug_prob"] == 1.0
    for bad, why in [({"learning_rate": 1}, "whitelisted"), ({"debug_prob": 1.5}, "outside"),
                     ({"max_stagnation": 2.5}, "integer"), ({"max_stagnation": 15}, "no-op"),
                     ({"max_stagnation": 5, "debug_prob": 0.5, "num_drafts": 3}, "1..2 keys")]:
        with pytest.raises(PatchError, match=why):
            apply_patch(rule_h, patch("decision_policy", {"op": "set", "values": bad}), "H1")


def test_decision_policy_text_only_in_agent_mode_and_rule_config_only_in_rule_mode(agent_h, rule_h):
    assert agent_h.rule_config is None and rule_h.decision_policy_text == ""
    with pytest.raises(PatchError, match="must be"):
        apply_patch(rule_h, patch("decision_policy", {"op": "append", "text": "x"}), "H1")
    a1 = apply_patch(agent_h, patch("decision_policy", {"op": "append", "text": "Prefer trustworthy validation."}), "H1")
    assert "Decision policy" in a1.render_notes() and "Prefer trustworthy" in a1.render_notes()
    assert "Decision policy" not in rule_h.render_notes()


def test_memory_policy_patch(agent_h):
    h1 = apply_patch(agent_h, patch("memory_policy", {"op": "set", "values": {"max_records": 2}}), "H1")
    assert h1.memory_policy["max_records"] == 2
    with pytest.raises(PatchError):
        apply_patch(agent_h, patch("memory_policy", {"op": "set", "values": {"max_records": 99}}), "H1")


@pytest.mark.parametrize("bad", [{}, {"target_component": "code", "proposed_patch": {"op": "x"},
                                     "expected_effect": "e", "evidence": ["e"]},
                                 {"target_component": "prompt", "proposed_patch": {"op": "append", "text": "x"},
                                  "expected_effect": "", "evidence": ["e"]},
                                 {"target_component": "prompt", "proposed_patch": {"op": "append", "text": "x"},
                                  "expected_effect": "e", "evidence": []}])
def test_patch_shape_is_validated(bad):
    with pytest.raises(PatchError):
        validate_patch(bad)


def test_h0_renders_empty_stock_notes(agent_h, rule_h):
    assert agent_h.render_notes() == "" and rule_h.render_notes() == ""


def test_store_versions_diff_and_git(state, agent_h):
    store = HarnessStore(state / "harness_versions", "t", "agent")
    store.create_initial(agent_h)
    h1 = store.commit_patch(agent_h, patch("prompt", {"op": "append", "text": "Verify the output."}))
    assert store.versions() == ["H0", "H1"] and store.latest() == "H1" and store.next_version() == "H2"
    vdir = store.version_dir("H1")
    diff = (vdir / "diff.patch").read_text()
    assert "--- H0/harness/prompt_notes.md" in diff and "+++ H1/harness/prompt_notes.md" in diff
    assert "+Verify the output." in diff                      # a file-level record, not an edit format
    assert json.loads((vdir / "patch.json").read_text())["target_component"] == "prompt"
    assert json.loads((vdir / "version.json").read_text())["parent"] == "H0"
    assert store.load("H1") == h1
    tags = subprocess.run(["git", "-C", str(state), "tag"], capture_output=True, text=True).stdout.split()
    assert {"harness/t/agent/H0", "harness/t/agent/H1"} <= set(tags)
    assert not (vdir / "NOT_COMMITTED.txt").exists()
    log = subprocess.run(["git", "-C", str(state), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "harness harness/t/agent/H1" in log


def test_invalid_patch_creates_no_version(state, agent_h):
    store = HarnessStore(state / "harness_versions", "t", "agent")
    store.create_initial(agent_h)
    with pytest.raises(PatchError):
        store.commit_patch(agent_h, patch("prompt", {"op": "append", "text": "/private/data"}))
    assert store.versions() == ["H0"]


def test_store_outside_git_degrades_to_a_warning(tmp_path, agent_h):
    store = HarnessStore(tmp_path / "hv", "t", "agent")
    store.create_initial(agent_h)
    assert (store.version_dir("H0") / "NOT_COMMITTED.txt").exists()



def test_a_legacy_single_json_version_still_loads(tmp_path):
    """The first pilot stored versions as one harness.json; those must remain readable."""
    legacy = Harness(task="t", mode="rule", version="H1", parent="H0", prompt_notes="old advice",
                     rule_config={"max_stagnation": 15, "debug_prob": 1.0, "max_debug_depth": 20, "num_drafts": 5},
                     memory_policy={"max_records": 5, "max_chars": 1500})          # no `render` key: pilot format
    vdir = tmp_path / "hv" / "t" / "rule" / "H1"
    vdir.mkdir(parents=True)
    (vdir / "harness.json").write_text(json.dumps(legacy.to_dict()))
    loaded = HarnessStore(tmp_path / "hv", "t", "rule").load("H1")
    assert loaded == legacy and loaded.memory_render == "lessons"


def test_a_directory_version_round_trips_exactly(state, agent_h):
    store = HarnessStore(state / "harness_versions", "t", "agent")
    store.create_initial(agent_h)
    h1 = store.commit_patch(agent_h, patch("prompt", {"op": "append", "text": "Line one.\nLine two."}))
    assert store.load("H1") == h1
    assert (store.version_dir("H1") / "harness" / "prompt_notes.md").read_text() == "Line one.\nLine two."
    assert not (store.version_dir("H1") / "harness.json").exists()


def test_file_diff_stays_a_valid_patch_when_a_file_has_no_trailing_newline():
    """Found on a real improver run: files written without a final newline glued the next file header onto their last line."""
    from rsi_mvp.harness import HarnessStore
    diff = HarnessStore.diff_files({"a.md": "old\n", "b.md": ""}, {"a.md": "new line without newline", "b.md": "x\n"}, "H0", "H1")
    assert "newline\n\\ No newline at end of file\n--- H0/harness/b.md" in diff
    import subprocess, tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        pathlib.Path(d, "a.md").write_text("old\n")
        pathlib.Path(d, "b.md").write_text("")
        pathlib.Path(d, "p.diff").write_text(diff.replace("H0/harness/", "a/").replace("H1/harness/", "b/"))
        r = subprocess.run(["patch", "-p1", "-i", "p.diff"], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert pathlib.Path(d, "a.md").read_text() == "new line without newline"
