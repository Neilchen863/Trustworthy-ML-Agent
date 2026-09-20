> **2026-09-20 - read this first.** Every `agent` row/claim below is **not** an agent-mode result. In every
> agent-mode run of the pilot and of v2 the decision-LLM call failed before it was sent
> (`[agent search] failed ('tuple' object has no attribute 'strip') -> falling back to rule-based policy`; 0 successful
> `LLM chose` lines), so those runs executed the stock rule policy. Cause: the default branch of
> `_inject_agent_decision.py::_ordered_selection_prompt` passes `[(title, text), ...]` tuples to the prompt compiler.
> It is not fixed. The `rule` rows are unaffected. v2 was started and then stopped (see the end of this file).
> Full picture: `docs/real_rsi_progress_20260920.md`.

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
* `validation_mirage` was -1 in all six runs. **Its magnitude, however, was never measured**: the first
  version of this note called the reward "saturated" (0.2-0.4% of nodes) and that was wrong - see
  "Deep-dive corrections", item 2 (agent mode: at least 10% of nodes; rule mode: 5.0% -> 2.8% -> 0.4%).

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

**2. `validation_mirage = -1` - CORRECTED. The first version of this item was wrong.**
It claimed the reward was saturated because only 0.2-0.4% of nodes were flagged. Those figures counted the
scanner's *exemplar findings*: `audit_run.py` keeps ONE finding per (detector, pattern) per run and writes the
real hit count into its message as `[共 N 个节点命中此模式]` (no suffix = 1). Counting findings therefore counts
patterns, not nodes. With the real hit counts (denominator = nodes with code; *lower* bound = the largest single
pattern, *upper* bound = the patterns summed and capped at 100%, because overlap between patterns is not
recoverable from the scanner's output):

| run | nodes | flagged, lower | flagged, upper |
| --- | --- | --- | --- |
| rule H0 | 500 | 5.0% | 6.2% |
| rule H1 | 500 | 2.8% | 2.8% |
| rule H2 | 500 | 0.4% | 0.8% |
| agent H0 | 466 | 11.8% | 24.2% |
| agent H1 | 66 | 9.1% | 13.6% |
| agent H2 | 500 | 10.0% | 26.6% |

So the behaviour was common in agent mode (at least 1 node in 10 under every harness) and the binary -1 was
mostly an accurate report, not noise. In rule mode the lower bound fell 5.0% -> 2.8% -> 0.4% across H0 -> H1 -> H2
while agent mode stayed near 10-12%. That is suggestive of a rule-mode effect, but it is one run per cell, so it
neither shows nor rules out an effect; the earlier "the patches did not change the behaviour" is withdrawn in
both directions. What survives: a worst-severity flag cannot tell 5% from 0.4%, so a per-node rate is still the
reward this design needed. What does not survive: "there was almost nothing to fix".

**3. Delivery is confirmed at prompt level:** the patch phrase occurs in `aide.verbose.log` twice per node
(1000 hits for 500 nodes), i.e. in every LLM call, not just in the staged notes file.

**4. My routing hid a coverage gap, mildly.** `E0_coverage_gap` (6-10 nodes/run whose `term_out` is
`<OMITTED>`, journal-level extraction audit blind, partly covered by the verbose-log check E1v) is routed
to no verifier, so "could not check" reads as `extraction_distortion = 0`.

## Follow-up (2026-09-20): what changed in the code (no new runs were submitted)

1. **Node-level rate reward.** `validation_mirage` is configured `unit: node`: reward = -(nodes flagged / nodes
   with code), taken from the scanner's real hit counts, with a lower bound (`rate`) and an upper bound
   (`rate_upper`). An aggregate finding that names no node keeps its severity and is never diluted. Other
   verifiers stay run-level because the scanner truncates their id lists. `actionable_rate: 0.01` decides what
   reaches memory and the meta-improver; the old severity reward is kept as `severity_reward`.
2. **Memory and patches separated.** `memory_policy.render` defaults to `none` for new harnesses: memory feeds the
   meta-improver only, so the agent sees advice only through a patch. Harnesses saved without the key (H0-H2 of the
   pilot) keep rendering lessons. `render` cannot be changed by a patch.
3. **Replicates and a pinned first draft.** `task.yaml` sets `replicates: [1,2,3]` and a neutral, leak-free seed
   (`tasks/random_acts_of_pizza/seeds/first_draft.py`, 5-fold AUC 0.6364 on the real data locally). The runner stages
   it content-addressed and immutable, and delivery is checked (first node's code, `seed_code` in `run_config`).
   The driver submits all replicates of a (mode, round) together and calls the meta-improver once over all of them.
   `rsi summarize` prints mean / sd / min / max and the mirage rate per round.
4. **Metric-guard fix written, not applied** - `patches/`. The guard that runs in a job is the copy inside the
   overlay, so the fix only takes effect after `08_patch_feedback_interface.sh` is re-run.

Starting the next experiment needs a fresh state root, because `rsi init` refuses to overwrite H0:
`rsi --state-root experiments/v2 init --task random_acts_of_pizza`.

## v2 outcome (2026-09-20): stopped, no usable comparison

v2 (rule and agent x 3 replicates x H0->H1->H2, pinned first draft, per-node rate reward, dedicated patched overlay)
was launched at 05:24 UTC with 6 jobs. Only two finished before the project owner stopped everything (about 07:50 UTC):
rule 1459602 (official 0.6687, mirage rate 1.4%) and agent 1459606 (official 0.6406; invalid, see the banner). Four jobs
were deleted while running. Verified while it ran: the dedicated overlay was mounted by all six jobs, the pinned seed
gave a bit-identical first node (0.636461) in the five runs that could be read, and no node had val > 1.0 (the
val = 5.0 artifact of the metric guard was gone). Spend: v2 $148.88, pilot $252.63, total about $401.6 at list price.
