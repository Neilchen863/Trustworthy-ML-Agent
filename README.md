# real-rsi-mvp

> **Status 2026-09-20:** agent-mode results so far are invalid (the decision LLM never ran; see `docs/results_roap_h0_h1_h2.md`).
> The rule arm and the pipeline itself are sound. No job is running on CRC.


Minimal RSI loop around the **existing** AIDE rule-based and agent-based modes on
MLE-bench tasks (implements *Real RSI engineering spec v0.3*, `docs/`).

```
task package -> existing AIDE runner (rule | agent) + mutable harness H_t
             -> trajectory + decision events -> verifier bank (reward vector)
             -> memory update -> improver agent edits the harness FILES in a workspace copy
             -> scope + runnability check -> H_{t+1}   (git diff = post-hoc record)
```

The mutable object is only the **harness**, a directory of files (`harness.ALLOWED_FILES`): prompt notes, a
mode-specific decision policy (agent mode: instruction text; rule mode: a whitelisted, bounded config), the memory
policy, and two optional **hook scripts** that implement harness responsibilities (`hooks/select_memory.py`: how
memory is retrieved; `hooks/render_notes.py`: how the context that reaches the agent is organised). The boundary is
by responsibility, not by file extension. AIDE, the task environment and the evaluator are never touched.

## How the harness is improved (2026-09-20 redesign: direct edits, not prescribed patches)

*What* is changed = the harness files above. *How* = the improver agent edits them directly.

1. `loop.improve` builds a private workspace: `harness/` (editable copy of H_t) + `context/` (read-only:
   `SUMMARY.md`, `runs.json`, `memory.json`, `history.json`, `INSTRUCTIONS.md`).
2. The improver (`rsi_mvp/improver.py`; `AgentImprover` = an LLM with `list/read/write/delete/check/finish` tools)
   edits `harness/` freely: no "one component per patch", no "append N characters".
3. The framework then checks **scope** (only `ALLOWED_FILES`; `context/` untouched; size, bounds, forbidden
   text) and **runnability** (JSON parses, hooks pass the static policy and actually render notes in the sandbox).
   Only a harness that passes becomes H_{t+1}; status is `proposed | no_change | rejected | error`.
4. `diff.patch` (file-level) and `improver_record.json` (summary, transcript, scope report) are written next to the
   version: a post-hoc record, never an input.

Hook scripts run in `rsi_mvp/hooks.py`'s sandbox: an AST policy (pure text/data helpers only; no `_private` or
frame/generator/introspection attributes, no `hash`/`id`); the script sees **facades** of the allowed stdlib modules
(public non-module attributes only, so `collections._sys` or `string.Formatter.get_field` cannot reach real module
namespaces); a fresh interpreter with empty env and an empty cwd; timeout, CPU/memory limits and `RLIMIT_NOFILE=3`
(no new file or socket can be opened even if a Python-level escape were found). This is defence in depth against
accidents of our own improver, **not** a security boundary against an adversary (that needs a container). The legacy
bounded-JSON-patch improver is kept as `--improver patch` (`PatchImprover`); `MockImprover` does deterministic
direct edits for offline runs.

**Reproducibility.** The check that a candidate harness "runs" uses the round and memory that version will actually
get, renders twice (different results = rejected) and happens before the commit. The rendered notes are then
**stored with the version** (`rendered_notes.txt`, sha256 in `version.json`); plan, collect, freeze and held-out use that
text and never re-execute a hook. Both render paths (default and hook) share one final gate: at most 20,000 characters,
no forbidden material.

A version's notes are a function of (harness, memory written before that version existed), so the notes checked
at collect time equal those submitted at plan time even while replicates are collected one by one. Switching
`memory_policy.render` to `lessons` is allowed (it is harness) but reported as a confound in the scope warnings.

## Spec -> code

| spec | where |
|---|---|
| 4 task package (`task.yaml`, `aide_config.yaml`, `verifier_config.yaml`, ...) | `tasks/<name>/`, `rsi_mvp/task.py` |
| 5 harness + versions + diff + git | `rsi_mvp/harness.py` -> `harness_versions/<task>/<mode>/H<n>/` |
| 6 unified decision events (both modes) | `rsi_mvp/events.py` (post-hoc from `journal.json` + `aide.log`) |
| 7 verifier bank, issue-scanner reuse | `rsi_mvp/verifiers/`, `vendor/issue_scanner/audit_run.py` (unmodified copy) |
| 8 verifier-as-reward, raw vector | `verifiers.run_bank` -> `reward_vector` (never collapsed) |
| 9 memory | `rsi_mvp/memory.py` -> `harness_versions/<task>/<mode>/memory.jsonl` |
| 10 meta-improver | `rsi_mvp/improver.py` (direct-edit agent, workspace, tools), `rsi_mvp/hooks.py` (hook sandbox), `rsi_mvp/meta_improver.py` (evidence + legacy patch), `rsi_mvp/llm.py` |
| 11 main loop, freeze, held-out | `rsi_mvp/loop.py`, `rsi_mvp/cli.py` |

Six of the seven spec verifiers are implemented by routing the scanner's detectors
(`issue_scanner_adapter.ROUTES`): `validation_argmax_anchoring`, `submission_sanity`,
`validation_mirage`, `extraction_distortion`, `simplified_error_attribution`,
`fallback_to_runnable`. `decision_insensitive_prompting` needs paired prompt A/B runs
and is reported as `not_applicable`, never silently dropped.

## Train / held-out discipline (enforced in code, `rsi_mvp/guards.py`)

* A task has `role: train | test`. Only **train** tasks may expose their official grade to the
  meta-improver; every exposure is appended to `rounds/exposure_ledger.jsonl`.
* A **test** task cannot be collected into a round, get a harness, or reach the meta-improver.
  Its runs are refused until `rsi freeze`, and may then only use `H0` or the frozen harness.
* `rsi heldout-report` re-reads the ledger and fails if a held-out competition was ever exposed.
* `freeze` snapshots the memory lessons, so later memory edits cannot change H_final; after a
  freeze, `collect`/`improve` for that harness are refused.
* Inner AIDE never sees test signal: `AIDE_FEEDBACK=0 AIDE_TEST_FEEDBACK=0 AIDE_TEST_EXPOSE=none`.

Shipped packages: `random_acts_of_pizza` (train) and `insults_heldout`
(`detecting-insults-in-social-commentary`, test). A different held-out task = another package
with `role: test`.

## Use

```bash
pip install pyyaml pandas pytest          # pandas: the scanner's submission checks
python -m pytest -q                       # 169 tests, no CRC / LLM / research repo needed

# offline plumbing check over archived runs (mock meta-improver; NOT an experiment)
python -m rsi_mvp replay-demo --task random_acts_of_pizza --mode rule --runs RUN_A RUN_B RUN_C
```

On CRC (login node of the research checkout; key sourced from your run.env, never pasted):

```bash
export MLEBENCH_AIDE_ROOT=~/mlebench-aide          # the MLE-bench_AIDE checkout
python -m rsi_mvp init   --task random_acts_of_pizza                       # H0 = stock AIDE, both modes
python -m rsi_mvp submit --task random_acts_of_pizza --mode agent --round 0 [--dry-run]
python -m rsi_mvp status
python -m rsi_mvp collect --task random_acts_of_pizza --mode agent --round 0 --job-id <ID>
python -m rsi_mvp improve --task random_acts_of_pizza --mode agent --round 0 --llm openai \
       [--improver agent|patch] [--max-steps 30]     # agent (default) edits harness files directly
#   ...repeat submit/collect/improve for round 1, 2 ...
python -m rsi_mvp freeze --task random_acts_of_pizza --mode agent
python -m rsi_mvp submit --task insults_heldout --mode agent --round -1 \
       --harness-task random_acts_of_pizza --harness H0        # and again with the frozen version
python -m rsi_mvp heldout-collect --task insults_heldout --mode agent --harness-task random_acts_of_pizza \
       --harness H0 --job-id <ID>                              # also for the frozen version
python -m rsi_mvp heldout-report --harness-task random_acts_of_pizza --mode agent
```

Unattended training rounds (resumable, halts on any anomaly, no freeze / held-out):

```bash
nohup python -m rsi_mvp drive --task random_acts_of_pizza --modes rule agent --last-round 2 > rounds/driver.out 2>&1 &
```

CRC notes: host python is 3.9 without pandas (use a venv with pyyaml+pandas); `sge/submit.sh` re-reads the
key from `config/env.sh` on the compute node, so that file must hold a valid key.

## Experiment design switches (2026-09-20)

* **Per-node rate reward** (`verifier_config.yaml: units`, `actionable_rate`): `validation_mirage` is the fraction
  of nodes flagged (lower bound `rate`, upper bound `rate_upper`), not "worst severity". Run-level verifiers are
  unchanged. Read `docs/results_roap_h0_h1_h2.md` for why (the first pilot mis-measured this).
* **Memory vs patch**: `rsi init --memory-render none` (default) keeps memory out of the agent's prompt so a patch's
  effect is not confounded with an auto-injected copy of the same advice; `lessons` reproduces the first pilot.
* **Replicates + pinned first draft**: `task.yaml` `replicates` and `pinned_first_draft`; `rsi drive` runs every
  replicate of a (mode, round) together, improves once, and `rsi summarize` reports mean/sd over replicates.
* Start a new experiment in a new state root: `rsi --state-root experiments/v2 init --task random_acts_of_pizza`.
* `patches/` holds the (unapplied) metric-guard fix and how to get it into the overlay.

## How H_t reaches AIDE (no edits to the research repo)

`runner.py` submits through the research repo's own `sge/submit.sh` using only channels it
already exposes; every variable set is in `scripts/_common.sh`'s caller-override list, so
`config/env.sh` cannot silently overwrite it (a test checks this against the real file).

* notes / decision policy / memory lessons -> a new immutable, content-addressed
  `config/tasks/<comp>.notes.rsi_<hash>.txt` + `PROMPT_VARIANT`. H0 renders empty, so it is the
  untouched stock control.
* rule config -> `AIDE_MAX_STAGNATION`, `AIDE_DEBUG_PROB`, `AIDE_MAX_DEBUG_DEPTH`,
  `AIDE_EXTRA_KWARGS=agent.search.num_drafts=N`.
* mode -> `AIDE_SELECTION_MODE=rule|agent`.

## Known limits (read before trusting a number)

* **One live pilot has been run** (ROAP, both modes, H0->H1->H2; see `docs/results_roap_h0_h1_h2.md`) - it
  demonstrates the pipeline, not that the patches help (n=1 per cell, large confounds listed there).
  Held-out evaluation has **not** been run.
* Decision-policy text lives in the task notes, so in agent mode it also appears in the
  code-generation prompts. In rule mode, decisions read no prompt: only the rule config and the
  notes (code generation) can change behaviour.
* Events are reconstructed post hoc. Rule mode has no reasoning, so its rationales are
  `rationale_source: "inferred"`; agent rationales are `"logged"` and truncated by AIDE's log.
* In rule mode the research repo does not start the per-node oracle grader, so the detectors
  that need per-node test scores (`S3`, `S4`) cannot fire; the final grade still exists.
* "Fixed seeds": AIDE/LLM calls are not bit-for-bit deterministic. `replicates` label matched
  runs (optionally with a pinned first draft). Only the offline pipeline (verifiers, memory,
  mock meta-improver, versioning) replays byte-identically.
* `MockMetaLLM` / `MockImprover` apply a fixed verifier -> edit table. They prove plumbing, not the method.
* The direct-edit `AgentImprover` is covered offline by `ScriptedToolLLM` only; it has **not** yet been run
  against a real model (the local key is disabled; run it on CRC first with `--max-steps` small).
* Verifier detectors that use hidden-test scores (`S3/S4/S7`) run only on train tasks before the
  freeze; held-out results are read only after it.
