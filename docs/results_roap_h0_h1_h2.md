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
* `validation_mirage` stayed at -1 in **all six** runs (same detector, `M2_fit_before_split`):
  in this task, adding text advice did not change the detected behaviour.

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

## Anomaly worth a separate look
Agent H0 and H2 submitted nodes whose recorded validation metric is 5.0 - impossible for AUC - yet
`extraction_distortion` scored 0 for both. Either a scanner blind spot (value printed by the program
itself) or a mis-parsed metric. Not investigated.
