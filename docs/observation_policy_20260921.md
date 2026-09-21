# Observation policy (AIDE_SUB_STATS as harness configuration) and stage-aware evidence

Decisions (user, 2026-09-21): (1) `AIDE_SUB_STATS` is inside the harness boundary; (2) the improver gets stage-aware evidence first (English is not
important); (3) test-side errors are not all "delete the feature".

## 1. `observation_policy.json`
Harness file `{"submission_profile": true|false}`; omitted = stock. It sets AIDE's existing `AIDE_SUB_STATS=1`, which appends a numeric profile of the
candidate's own `submission.csv` (rows, per column n_unique / min / max / mean / std / NaN) to each node's output. Boundary by responsibility, not by where the switch
lives: it is evidence display (context configuration), not AIDE code. Checked: the patch (`scripts/_inject_submission_stats.py`) reads only
`workspace/submission/submission.csv`, never labels, scores or the grader (test `test_the_patch_only_reads_the_candidates_own_submission_file`); the improver's
instructions say so too.

**Not in the research repo's caller-override whitelist** and we do not edit that repo. Reason it is still safe: nothing in `config/env.sh` or `config/tasks/*.sh` sets it
(tested where the repo is present), and, more importantly, **collect verifies the switch in both directions** (`trajectory.delivery_check`): enabled -> the run config
must say `sub_stats = 1` and nodes must be logged as profiled (`[sub-stats] profile appended`), else `delivery.ok = false`; not enabled -> no profile may appear
(a hidden confound otherwise). A failed check blocks `improve` like any undelivered harness.

**Verified on CRC** (smoke job 1460936, H1 with the setting on, 3 nodes, $0.105, overlay v3): run config `sub_stats = 1`; `[sub-stats] profile appended` for 3/3 nodes;
profile text in the journal for 1/3 nodes (the journal omits most node outputs, so it can only confirm); collect `delivery.ok = true` with the evidence recorded. All earlier
runs in this project have `sub_stats = off` (so none of the earlier failures had the profile).

### What the profile does and does not reach (measured on that run's logged prompts)
* It appears in the **feedback-review prompt** of every executed node (3 of 78 logged prompts).
* It does **not** appear in the **submit-choice prompt**: that prompt shows, per candidate, id / stage / validation metric / cv fields / plan / findings (the reviewer's text), no
  code and no node output. So `AIDE_SUB_STATS` reaches the submission-choosing LLM only if the reviewer repeats it in its findings. In these 3 healthy nodes it did not
  (nothing to flag). Whether the reviewer flags a degenerate profile (n_unique = 1) is untested.
* This corrects my earlier suggestion ("surface the prediction profile at selection time via AIDE_SUB_STATS"): the switch exposes it to the reviewer, not directly to the decider.
  If the decider must see it, either the reviewer has to pass it on, or the decision prompt has to carry it (an AIDE change, outside this boundary).

## 2. Stage-aware evidence (`rsi_mvp/stage_trace.py`)
At collect (train tasks only), when the leaky-field detector names a field, the run record gets a `stage_trace`: field first used -> crash on the test side -> repair ->
perfect validation feedback -> degraded test predictions -> final choice, each with journal steps and evidence, plus what the submission-choosing LLM could read
(measured on its logged prompt: candidate fields shown, code shown, prediction profile shown, field / KeyError mentioned, chosen node's feedback visible and praising) and
which facts were **not** visible to it, and where the profile actually appeared (`profile_exposure`). It reads only the journal, archived candidate predictions, and logs
(never the grade). It reaches the improver in `SUMMARY.md` and `runs.json`, and the instructions tell it to separate "instruction too vague" from "information never shown".
On the failed H0 run it reproduces the hand analysis: steps 1 -> 1,3,13 (KeyError) -> 4 (guard) / 7 -> 7 (1.0) -> 16 and 20 constant -> 20 chosen; the decision prompt
showed no code, no profile, no KeyError.

## 3. Guidance about test-side errors
The improver's instructions now say: a KeyError on test data can also be a column-name or preprocessing mismatch; guidance should be to establish first whether the feature is
available at prediction time, remove it and re-validate if it is train-only, and never hide the problem with a test-side default. (The earlier note "drop it, never default it"
in `v3_failure_mechanism_20260921.md` is superseded by this wording.)

Fixed H1 is unchanged (its version and rendered notes are untouched).
