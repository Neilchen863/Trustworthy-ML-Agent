# real-rsi-mvp

Minimal RSI loop around the **existing** AIDE rule-based and agent-based modes on
MLE-bench tasks (implements *Real RSI engineering spec v0.3*, `docs/`).

```
task package -> existing AIDE runner (rule | agent) + mutable harness H_t
             -> trajectory + decision events -> verifier bank (reward vector)
             -> memory update -> meta-improver -> bounded patch -> H_{t+1}
```

The mutable object is only the **harness**: prompt notes, a mode-specific decision
policy (agent mode: instruction text; rule mode: a whitelisted, bounded config), and a
memory policy. AIDE, the task environment and the evaluator are never touched.

## Spec -> code

| spec | where |
|---|---|
| 4 task package (`task.yaml`, `aide_config.yaml`, `verifier_config.yaml`, ...) | `tasks/<name>/`, `rsi_mvp/task.py` |
| 5 harness + versions + diff + git | `rsi_mvp/harness.py` -> `harness_versions/<task>/<mode>/H<n>/` |
| 6 unified decision events (both modes) | `rsi_mvp/events.py` (post-hoc from `journal.json` + `aide.log`) |
| 7 verifier bank, issue-scanner reuse | `rsi_mvp/verifiers/`, `vendor/issue_scanner/audit_run.py` (unmodified copy) |
| 8 verifier-as-reward, raw vector | `verifiers.run_bank` -> `reward_vector` (never collapsed) |
| 9 memory | `rsi_mvp/memory.py` -> `harness_versions/<task>/<mode>/memory.jsonl` |
| 10 meta-improver | `rsi_mvp/meta_improver.py`, `rsi_mvp/llm.py` |
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
python -m pytest -q                       # 83 tests, no CRC / LLM / research repo needed

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
python -m rsi_mvp improve --task random_acts_of_pizza --mode agent --round 0 --llm openai
#   ...repeat submit/collect/improve for round 1, 2 ...
python -m rsi_mvp freeze --task random_acts_of_pizza --mode agent
python -m rsi_mvp submit --task insults_heldout --mode agent --round -1 \
       --harness-task random_acts_of_pizza --harness H0        # and again with the frozen version
python -m rsi_mvp heldout-collect --task insults_heldout --mode agent --harness-task random_acts_of_pizza \
       --harness H0 --job-id <ID>                              # also for the frozen version
python -m rsi_mvp heldout-report --harness-task random_acts_of_pizza --mode agent
```

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

* **Not yet run live on CRC from this repo.** The submit path is tested against a stub
  `submit.sh`; the collect/verify path was smoke-tested on archived real ROAP runs.
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
* `MockMetaLLM` applies a fixed verifier -> patch table. It proves plumbing, not the method.
* Verifier detectors that use hidden-test scores (`S3/S4/S7`) run only on train tasks before the
  freeze; held-out results are read only after it.
