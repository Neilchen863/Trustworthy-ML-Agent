Version 0.3 · Minimal implementation for MLE-Bench tasks

Goal. Build a small system around the existing AIDE runner: run a fixed MLE task, score the resulting trajectory with reusable verifiers, store compact experience memory, and use these signals to patch the AIDE harness for the next RSI round.

# 1. Scope

- Start with CPU-only MLE-Bench tasks (for example, ROAP-like tabular tasks).

- Each task has a fixed environment: instruction, data, dependencies, execution entrypoint, and evaluator.

- The mutable object is the AIDE harness used for that run: prompt/instruction, mode-specific decision policy/configuration, and memory.

- Reuse AIDE's existing rule-based and agent-based modes. Do not reimplement either mode in this project.

- Do not implement recursive verifier improvement, environment modification, or GPU scheduling in v0.3.

# 2. System Architecture

Task package (task by task)\
- fixed environment\
- aide_config.yaml \# existing AIDE mode + task-specific settings\
- verifier_config.yaml \# enabled verifiers / thresholds\
↓\
Existing AIDE runner (rule-based or agent-based)\
+ mutable harness H_t\
↓\
Run task → trajectory\
↓\
Verifier Bank → reward + evidence + explanation\
↓\
Memory update\
↓\
Meta-improver patches H_t → H\_{t+1}

# 3. Components

  ----------------------------------------------------------------------------------------------------------------------------------------------------------------------
  Component               Responsibility                                                                                Editable by RSI?
  ----------------------- --------------------------------------------------------------------------------------------- ------------------------------------------------
  Task Package            Fixed environment plus task-local AIDE and verifier configuration.                            Environment: No; configs: fixed per experiment

  Existing AIDE Mode      Existing rule-based or agent-based execution mode selected by each task.                      Do not reimplement

  Harness                 Prompt/instruction, mode-specific policy/config, and memory used by the selected AIDE mode.   Yes

  Memory                  Compact records of situation, decision, evidence, outcome, verifier finding, and lesson.      Yes

  Verifier Bank           Global reusable detectors; configured task by task; emits reward + evidence + explanation.    No in v0.3

  Meta-improver           Reads runs + memory + verifier rewards/findings and proposes a bounded harness patch.         N/A
  ----------------------------------------------------------------------------------------------------------------------------------------------------------------------

# 4. Task Environment

**Recommended layout:**

tasks/\<task_name\>/\
instruction.md\
env/ \# requirements / container config\
data/\
run.py \# standard task entrypoint\
evaluate.py \# fixed final evaluator\
task.yaml \# budget, metric, paths, limits\
aide_config.yaml \# rule-based \| agent-based + task settings\
verifier_config.yaml \# verifiers enabled for this task

The task environment is reproducible and fixed across H0, H1, \... runs. Each task also owns its AIDE configuration and verifier configuration, so experiments can be defined task by task without duplicating the global implementations.

# 5. Harness

Existing AIDE codebase\
rule-based mode \# already implemented\
agent-based mode \# already implemented\
\
harness_versions/\<task\>/\<mode\>/\
prompt / mode-specific policy config\
memory.jsonl\
version metadata + diff

Rule-based mode: reuse the existing AIDE rule-based implementation. RSI may patch only the whitelisted rule/policy configuration that belongs to the harness.

Agent-based mode: reuse the existing AIDE agent-based implementation. RSI may patch the prompt/policy instructions and memory interface used by the decision agent.

# 6. Trajectory and Decision Events

Instrument the existing AIDE modes rather than building a new policy layer. Both modes should emit the same structured decision-event schema so the same verifier interface can consume their trajectories.

{\
\"step\": 12,\
\"state_summary\": \"best val=0.812; latest candidate failed\",\
\"decision_type\": \"FAILURE_DIAGNOSIS\",\
\"action\": \"fallback_to_previous_model\",\
\"rationale\": \"\...\",\
\"evidence\": \[\"run_11.log\", \"metrics_11.json\"\],\
\"outcome\": null\
}

Initial decision types: EXPERIMENT_PROPOSAL, EXPERIMENT_LAUNCH, RESULT_INTERPRETATION, FAILURE_DIAGNOSIS, CANDIDATE_SELECTION, FALLBACK, MEMORY_WRITE, FINAL_SELECTION, SUBMISSION.

# 7. Verifier Bank

Verifier Bank is global and reusable; each task selects which verifiers to run through verifier_config.yaml. Reuse existing detectors where possible, especially the previous issue scanner, by wrapping them behind the common verifier interface instead of rewriting their logic.

  ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  **Verifier**                        **What it checks**
  ----------------------------------- ------------------------------------------------------------------------------------------------------------------------------------------
  validation_argmax_anchoring         Detect final choice following validation argmax while contradictory evidence is ignored; reuse issue-scanner logic if already available.

  submission_sanity                   Checks prediction range, output distribution, format, row alignment, NaNs, etc.

  validation_mirage                   Detects leakage, metric mismatch, faulty split/evaluation, or unstable validation.

  extraction_distortion               Recorded/quoted results differ from actual experiment artifacts; prefer existing issue-scanner checks where available.

  simplified_error_attribution        Failure is attributed to generic overfitting without checking more direct causes.

  fallback_to_runnable                A promising failed approach is abandoned without reasonable diagnosis/debugging.

  decision_insensitive_prompting      Additional evidence changes generation but does not affect the final decision.
  ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

Recommended layout and verifier output schema:

verifiers/\
issue_scanner_adapter.py\
submission_sanity.py\
validation_mirage.py\
\...\
\
{\
\"verifier\": \"validation_argmax_anchoring\",\
\"decision_steps\": \[31\],\
\"reward\": -1.0,\
\"evidence\": \[\"\...\"\],\
\"explanation\": \"Selection ignored conflicting evidence and followed validation argmax.\"\
}

# 8. Verifier-as-Reward

Use verifier outputs directly as RSI reward signals. Do not introduce a separate high-level reward taxonomy in v0.3, and do not immediately collapse all verifier rewards into one weighted scalar.

reward(H) = {\
task_performance,\
validation_argmax_anchoring,\
submission_sanity,\
validation_mirage,\
extraction_distortion,\
simplified_error_attribution,\
fallback_to_runnable,\
\...\
}

Each verifier returns its own reward. Keep the raw reward vector so the meta-improver can see which failure modes improved or regressed across RSI rounds. Task performance remains a separate task-level reward.

# 9. Memory

Memory is the compact experience carried across RSI rounds. Store one record per important episode, not the full transcript.

{\
\"situation\": \"small val gain; high seed variance\",\
\"decision_outcome\": \"accept candidate → test degraded\",\
\"verifier\": \"validation_argmax_anchoring\",\
\"reward\": -1.0,\
\"evidence\": \"candidate selected mainly because it had the highest validation score\",\
\"lesson\": \"check conflicting evidence before final selection\"\
}

# 10. Meta-Improver

After each task/run batch, call an LLM with: current AIDE harness, recent trajectory summaries, verifier outputs (reward + evidence + explanation), task performance, and relevant memory. It returns a small patch to the selected mode's harness rather than rewriting AIDE or the task environment.

Input: H_t + verifier rewards/findings + task performance + memory\
Output:\
- target component: prompt \| mode-specific decision policy/config \| memory policy\
- proposed patch\
- expected effect\
- evidence from current runs

Apply the patch to create H\_{t+1}. Version every harness in Git and save the exact diff.

# 11. Main RSI Loop

for round t in 0..T:\
for task in task_set:\
load fixed task env + aide_config + verifier_config\
run existing AIDE mode with H_t\
collect trajectory + artifacts\
run configured verifiers\
collect verifier rewards + task performance\
update compact memory\
ask meta-improver for bounded harness patch\
save H\_{t+1}\
\
freeze final harness\
evaluate H_0 vs H_final on held-out tasks / matched seeds

# 12. v0.3 Acceptance Criteria

- At least one CPU-only task can be created and reproduced from a clean environment.

- Both existing AIDE modes can be invoked through the same task wrapper; neither mode is reimplemented.

- Every run emits structured decision events, artifacts, and final task score.

- At least three verifiers run automatically and return reward + evidence + explanation; at least one should reuse an existing issue-scanner detector where applicable.

- Each task can independently select its AIDE mode/settings and verifier set through aide_config.yaml and verifier_config.yaml.

- Memory persists across RSI rounds and the meta-improver can create a versioned harness patch for the next round.

- A small H0 → H1 → H2 experiment can be replayed end-to-end with fixed seeds.

# 13. Recommended Implementation Order

1.  Create one CPU task package with task.yaml, aide_config.yaml, and verifier_config.yaml.

2.  Add a thin adapter around the existing AIDE rule-based / agent-based modes and instrument a unified decision-event logger.

3.  Wrap the existing issue scanner behind the verifier interface; reuse its detectors where applicable.

4.  Add the remaining minimal verifiers needed for the first task, e.g. submission sanity and validation anchoring.

5.  Add memory store + meta-improver + harness versioning, then run an end-to-end H0 → H1 → H2 pilot.
