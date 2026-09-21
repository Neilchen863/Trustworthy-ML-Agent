# v3: one real agent-mode run + one real improver session (2026-09-20)

Goal (user): restart the experiment, run **agent-based only, once**, total cost well under $10, no fast iteration.

## What was done
| step | result | cost (list price) |
|---|---|---|
| Fix the agent decision bug in a **dedicated** overlay `overlays/v3` (= v2 overlay + `patches/apply_agent_decision_fix.py`); shared overlay untouched | exactly 1 file differs from v2; fixed function returns `title -> text`, shared overlay control still returns lists | $0 |
| Smoke, job 1460024 (3 steps) | 3x `LLM chose`, 0x `falling back`, v3 overlay mounted, metric recorded 0.636 | $0.096 |
| **Real run, job 1460025** (H0, stock harness, `experiments/v3/tasks`: 50 steps / 1 h / 10 min per exec) | 22 nodes (10 valid, 12 buggy), 31 decision calls all by the LLM, 0 fallbacks; official AUC **0.65121** | $1.185 |
| Cost watchdog (cap $7.5) | never triggered | - |
| Collect round 0 | delivery ok; all 6 verifier rewards 0 (no failure mode detected) | $0 |
| Real improver session (`AgentImprover`, gpt-4o, cap 20 steps / $1) | 3 tool calls (read SUMMARY.md, list_files, read runs.json, finish), status `no_change`: "no detected failure modes, no changes justified" | $0.014 |
| **Total** | | **≈ $1.30** |

No H1 was produced (`no_change`), so no second run was made.

## What this does and does not show
* Agent mode ran with a real decision LLM for the first time in this project (pilot/v2 agent runs were rule fallbacks, see
  `results_roap_h0_h1_h2.md`). Search actions: 17 improve, 4 debug, 1 draft: the agent stayed on the single pinned-seed lineage.
* Wall clock, not the step cap, ended the run: 52 of 57 minutes were candidate execution (mean 143 s, two 600 s timeouts).
  12 of 22 nodes were buggy (6 ValueError, 2 TimeoutError, 4 other).
* One run, one score: **no claim about any harness effect**. There is nothing to compare H0 against.
* The direct-edit improver ran end to end against a real model, but with no failure evidence it (correctly) changed nothing, so the
  write / check / finish-with-changes path is still only tested offline (ScriptedToolLLM).
* Artifacts: run dirs `runs/random-acts-of-pizza/20260920_085624_..._j1460024` (smoke) and `..._085833_..._j1460025`;
  state `experiments/v3/`, `experiments/v3_smoke/`. The v3 overlay (1 GB) stays on CRC at `~/real-rsi-mvp/overlays/v3/`.
* CRC repo was backed up to `~/real-rsi-mvp-backup-pre-v3.tgz` before code was synced; `experiments/v2` and `overlays/v2` untouched.

## Follow-up: real improver on real failure evidence (mechanism check, ≈ $0.054)
Run in a scratch state root outside the git repo (`experiments/v3_improver_check/`, copied from v2, so no tags of the pilot were touched),
evidence = the v2 agent round-0 record (mirage rate [0.108, 0.174], `fallback_to_runnable` -0.25). **Those rewards are partly artifacts of the
old guard/fallback bugs, so this checks the mechanism only, not whether the edits help.**

gpt-4o (6 calls, 18k tokens in, cap $0.3): read `SUMMARY.md`, `list_files`, read `runs.json`, read the 3 harness files, **write 3 files**
(`prompt_notes.md`, `decision_policy.md`, `memory_policy.json`), `check()` ok, `finish` -> framework committed **H1** with a file-level diff,
`improver_record.json` and persisted `rendered_notes.txt`. So the direct-edit path (read -> write -> check -> scope/runnability -> commit) is
verified against a real model.

What the output shows, uncomfortably:
* The model flipped `memory_policy.render` from `none` to `lessons` (allowed by design; the scope report flagged it as a confound). The rendered
  notes then contain its own advice **twice** (once as notes/policy, once as auto-injected lessons), the same duplication that made pilot effects
  unattributable. Whether an improver may switch memory rendering on was decided by the user on 2026-09-21: it may.
* Its edits are text reminders (fit preprocessing inside the fold; do not abandon neural networks after an error), like the pilot's patches: a
  plausible response to the evidence, with no reason to expect a changed behaviour.
* Bug found and fixed in the same session: files written without a trailing newline glued the next file header to their last line in
  `diff.patch`, making it an invalid patch. `diff_files` now emits `\ No newline at end of file`; a test applies the diff with `patch`.
