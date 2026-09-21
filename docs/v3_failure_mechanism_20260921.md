# What happened in the one failed H0 run (v3_pairs, pair 1, job 1460465): a free trace

Single run, single event. Reproduce with `python tools/trace_failure_lineage.py runs/random-acts-of-pizza/20260920_135930_gpt4o_2h_nohint_j1460465`.
Nothing here says how often this happens or whether H1 prevents it.

## The chain, stage by stage
| stage | what happened (node indices) | evidence |
|---|---|---|
| 1. a train-only field enters | node 1 (an improvement of the pinned draft, node 0) builds `has_shroom_flair` / `has_pif_flair` from `requester_user_flair`; it is a known label-revealing field that the test data does not contain | node 1-3 code; `M7_leaky_field` verifier |
| 2. the crash that exposes it is repaired the wrong way | nodes 1, 2, 3 crash with `KeyError: 'requester_user_flair'` (the feedback LLM even states "not found in the test dataset"). Node 4's debug "fix" does `if column in df.columns: ... else: flag = 0` ("Assume no flair present in test data") instead of dropping the feature | node 4 code L47-53 |
| 3. validation becomes meaningless | node 7 (child of 4) scores CV AUC **1.0**; the feedback LLM calls it "perfect ... indicating an excellent performance"; nothing questions it | node 7 analysis |
| 4. the search stays in the contaminated lineage | 16 of 25 nodes build the flair flags; 5 nodes have val >= 0.999 and 13 descendants (52% of the search budget) | verifier evidence `S1_phantom_nodes`, `M7_leaky_field` |
| 5. test predictions collapse | every flair-using valid node has degenerate test predictions: node 7 range 0.0007-0.0049 (std 0.0002), node 8 only 42 distinct values, node 10 77, nodes **16 and 20 exactly constant (0.0987)**; honest nodes have std 0.07-0.28 and >1100 distinct values | per-node `submissions/*.csv` |
| 6. the submission is chosen | the decision LLM (twice) picks node 20 (val 1.0, the constant one) over honest 0.63-0.65 nodes, e.g. nodes 14/15 (0.653, no flair). Its prompt contains the task text and, per candidate, id / stage / validation metric / plan and findings (truncated). The findings say "perfect cross-validation ROC AUC of 1.0, indicating excellent model performance". It contains **no code, no test-prediction profile, no field-availability information** | verbose-log submit prompt (22k chars): 0 mentions of "leak", "distinct", "distribution" |

## Two problems, not one
* **Perfect validation** (stages 1-4) is a *leak entering and persisting*: a train-only field, kept alive by a crash-repair that defaults it, then rewarded
  by max-validation search.
* **Constant predictions** (stage 5) is a *consequence that shows up per model*, not automatically: the same leak gave a logistic regression a tiny but
  non-constant range (node 7) and gradient-boosted trees an exactly constant output (nodes 16, 20; a pure split on the flag leaves nothing else to use).
  It is visible in each node's own submission file from the moment the node runs, yet no LLM sees it: the research repo's neutral submission-statistics patch
  (`scripts/_inject_submission_stats.py`, `AIDE_SUB_STATS`, default OFF, written after exactly this failure in an earlier o3 run) was not enabled in these runs.
  The decision LLM therefore had no route to notice it, and the earlier project notes say a prompt instruction to self-check "cannot fix" that blind spot.
* Which node would have been submitted if the decision had avoided node 20 is unknowable here (no per-node test grades); node 7-11 predictions are non-constant
  but built on the same leaked feature.
* Only this run did it: in the other four v3_pairs runs (and the 22-node v3 run) no node mentions `requester_user_flair` at all, so the branch point is
  stage 1 (rare here), not a general property of every run.

## What the current verifiers give the improver (checked by building its evidence for this run)
Actionable and correct: `validation_argmax_anchoring -1` (submitted node is in a phantom lineage; 5 nodes val >= 0.999 while the test score is 0.5),
`submission_sanity -1` (`B1_constant_pred`, nunique 1), `validation_mirage -0.64` (16/25, `M7_leaky_field`, names `requester_user_flair`). So the *facts* reach it.
Not delivered: the stage structure. Nothing tells it that the leak survived because a crash was repaired by defaulting the field, that the feedback LLM praised
the perfect score, or that the decision prompt carried no prediction profile. Text is terse, in Chinese, and each verifier gives an exemplar step (`step 7`)
though the field first appears at node 1. `S7_val_beats_gold` and `S1_submitted_phantom` use official test information; that is allowed for the improver of a
train task, and such a verifier could not be used as a live signal inside the run.

## Candidate next changes (superseded 2026-09-21; see `observation_policy_20260921.md`)
User decisions: `AIDE_SUB_STATS` is inside the harness boundary (implemented as `observation_policy.json`, verified per run); stage-aware evidence goes to the improver first
(implemented); guidance about test-side errors must not say "delete every feature that raises a KeyError" - first establish whether the feature exists at prediction time,
remove it and re-validate if it is train-only, never mask it with a test-side default (a KeyError can also be a name/preprocessing mismatch).
Correction to an earlier point: the profile switch reaches the feedback reviewer, not the submit-choice prompt (measured), so it helps the decision only if the reviewer passes it on.
