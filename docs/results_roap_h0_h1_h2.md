# ROAP H0 -> H1 -> H2 pilot (2026-09-19, CRC) - what it does and does not show

Task `random_acts_of_pizza` (train), gpt-4o-2024-08-06 for code/feedback/meta-improver, 4h / 500 steps per
run, one run per (mode, harness). Driven unattended by `rsi drive`. **No freeze, no held-out was run.**

| mode | harness | official AUC | val of submitted node | nodes (working) | verifiers < 0 |
|---|---|---|---|---|---|
| rule  | H0 | 0.62227 | 0.6996 | 500 (445) | mirage |
| rule  | H1 | 0.57471 | 0.7483 | 500 (395) | mirage |
| rule  | H2 | 0.62800 | 0.6901 | 500 (399) | mirage |
| agent | H0 | 0.62724 | 5.0 (!) | 466 (251) | anchoring, mirage |
| agent | H1 | 0.62051 | 0.7018 | 66 (45)   | mirage |
| agent | H2 | 0.62411 | 5.0 (!) | 500 (250) | anchoring, mirage |

## What the patches were
All six patches (3 per mode; H1 and H2) were real gpt-4o proposals that passed the bounded-patch
validation. Every one is a text `append` about preprocessing leakage ("fit preprocessing inside the
training fold / after the split, avoid fields unavailable at prediction time, distrust a too-good
validation score"). Rule-mode patches target `prompt`; agent-mode patches target `decision_policy`.
No patch touched rule config, memory policy, or anything about *how to pick the submission*.

## What it shows
* The pipeline works end to end and unattended: submit -> wait -> collect -> verify -> memory ->
  meta-improver -> versioned patch -> next round, with delivery checked in every run
  (`prompt_variant` + notes present in the agent-read task description).
* `validation_mirage` was -1 in all six runs - **but see "Deep-dive corrections" below: that reward is
  saturated (one flagged node out of ~400 is enough), so it says nothing about whether the patches
  changed the behaviour.**

## What it does NOT show (confounds - do not read the score column as a patch effect)
* n = 1 per cell. Official AUC ranges 0.575-0.628 with no monotone pattern.
* Search volume is not comparable across runs: agent H1 executed 66 nodes (median candidate 30 s, five
  20-minute timeouts, 3.8 h of the 4 h spent executing) versus 466-500 nodes elsewhere (median 1-2 s).
  Heavier model families explain it, not the patch (the known first-draft "framework lottery").
* Agent `anchoring` was 0 in H1 only because that run had no phantom (val >= 0.999) node at all; the
  submitted node was still the validation argmax.
* The patch and the auto-injected memory lesson say nearly the same thing, so H_t = the same advice
  repeated. A patch's effect cannot be separated from the memory's.
* Official grades of these training runs were shown to the meta-improver (train task, logged in
  `rounds/exposure_ledger.jsonl`); nothing here is held-out evidence.

## Deep-dive corrections (2026-09-20, supersede the claims above where they conflict)

**1. The `val = 5.0` nodes are a metric-guard bug, not an agent or scanner failure.**
`scripts/_metric_parser_runtime.py` (research repo, the deterministic metric guard) takes, for a "CV
summary" line, the *first number after the last metric name*. On `Mean AUC score (5-fold CV): 0.6222...`
that is the `5` of "5-fold". Reproduced with the real parser: `Mean AUC over 5-fold CV: 0.6323` -> 5.0.
The node's own log line says so: `[metric-guard] source=cv_summary value=5.0 cv_n=0
corrected_reviewer_value=0.6323` - the reviewer LLM had the right number and the guard overrode it.

| run | guard overrode | recorded vs true differ by >0.05 | submitted: recorded -> true | official |
|---|---|---|---|---|
| rule H0/H1/H2 | 1 / 0 / 0 | 0 / 0 / 0 | true = recorded | .622 / .575 / .628 |
| agent H0 | 18 | 11 | 5.0 -> 0.6249 | 0.6272 |
| agent H1 | 0 | 0 | 0.7018 -> 0.7018 | 0.6205 |
| agent H2 | 26 | 16 | 5.0 -> 0.6323 | 0.6241 |

Consequences: (a) agent `validation_argmax_anchoring = -1` in H0/H2 is mostly this artifact (the "phantom"
nodes are the 5.0 nodes), and agent H1's `0` is only "the guard did not fire", not a patch effect;
(b) the submitted agent nodes were honest (true val ~= official) - the bug hijacked the argmax, it did not
cause an overfit submission; (c) whether the higher true-val nodes (H0 0.8062, H2 1.0000 "training AUC",
which the scanner did not flag) were better or worse officially is unknown; (d) the scanner's
`extraction_distortion` missed 5.0 - hypothesis, not verified: the digit `5` also appears in "5-fold", so
"recorded value present in stdout" passes. Any historical run using this guard may carry the same artifact.

**2. `validation_mirage = -1` is saturated; the earlier "patches did not change the behaviour" claim is
withdrawn.** Nodes hit by `M2_fit_before_split` / any M detector, of working nodes:
rule H0 1 (0.2%) / 3 (0.7%), H1 1 / 1, H2 0 / 1; agent H0 1 / 5 (2.0%), H1 1 / 3 (6.7%, n=45), H2 1 / 2.
The behaviour was already ~0.3% at H0, so there was almost nothing to fix, and a binary "worst severity"
reward cannot show a change between 0.2% and 0.3%. The meta-improver optimised a saturated signal. A
per-node rate, not a worst-severity flag, is the reward this pilot needed.

**3. Delivery is confirmed at prompt level:** the patch phrase occurs in `aide.verbose.log` twice per node
(1000 hits for 500 nodes), i.e. in every LLM call, not just in the staged notes file.

**4. My routing hid a coverage gap, mildly.** `E0_coverage_gap` (6-10 nodes/run whose `term_out` is
`<OMITTED>`, journal-level extraction audit blind, partly covered by the verbose-log check E1v) is routed
to no verifier, so "could not check" reads as `extraction_distortion = 0`.
