# v3_pairs results: fixed H1 vs stock H0, two pairs (run 2026-09-20/21)

Pre-registered in `experiments/v3_pairs/PREREG.md` (commit 8946564) before any run. Machine-readable table: `experiments/v3_pairs/RESULTS.json`.
Same configuration for all four runs (agent mode with the decision-prompt fix, gpt-4o, pinned first draft, 25 nodes, 10 min per execution);
only PROMPT_VARIANT differs (checked by dry run before submitting). Pair k = H0 and H1 submitted together.

## Results
| pair | arm | official AUC | mirage nodes flagged | good / buggy nodes | timeouts | exec min | cost |
|---|---|---|---|---|---|---|---|
| 1 | H0 | **0.5000** | **16 / 25** (64%) | 15 / 10 | 0 | 9 | $1.43 |
| 1 | H1 | 0.6030 | 0 / 25 | 18 / 7 | 2 | 46 | $1.60 |
| 2 | H0 | 0.6679 | 0 / 25 | 19 / 6 | 4 | 82 | $1.53 |
| 2 | H1 | 0.6439 | 0 / 25 | 13 / 12 | 5 | 93 | $1.44 |

AUC difference H1 - H0: **pair 1 +0.103, pair 2 -0.024, mean +0.039.** All four scores are shown; none was dropped.
Validity (pre-registered): all four runs graded with a valid submission, delivery confirmed, 0 decision fallbacks (36-42 LLM decisions each),
25 nodes each, none killed by the cost watchdog. Both pairs valid; H1 was not "clearly worse", so pair 2 ran as pre-registered.
Cost of the experiment **$5.99** (cap $7.00); with the earlier work in this session (about $1.35) the total is about **$7.34**, under $10.

## What happened in the one bad run (pair 1, H0)
Several nodes reported cross-validation AUC of 1.0 (leakage signature; 16 of 25 nodes were flagged, largely descendants of leaky nodes). The decision
LLM chose a node with CV AUC 1.0 ("perfect ... indicating excellent model performance"); its test predictions were one constant value, official AUC 0.5.
Verifiers fired correctly: `validation_argmax_anchoring` -1, `submission_sanity` -1, `validation_mirage` -0.64. This is the failure family
H1's notes warn about, and the first time it was seen in agent mode with a working decision LLM. The other stock-H0 runs under this fixed setup
(job 1460580 here, and job 1460025 earlier) did not show it: 1 of 3.

## How to read it (pre-registered limits apply)
* The mean AUC difference (+0.039) is below the ~0.055 that this design can resolve (run-to-run sd about 0.028), and it comes entirely from
  H0's single catastrophic run; in pair 2 H1 was lower. Two pairs with opposite signs are what "no effect" typically looks like.
* Mirage: 0/50 nodes for H1 vs 16/50 for H0, but the events are bursty (one leaky lineage contaminates many nodes), so the effective sample
  is runs: **H0 1 of 2 runs affected, H1 0 of 2.** Consistent with H1 helping and equally with chance.
* Diagnostics: H1 arms used more execution time in pair 1 (46 vs 9 min) and had more timeouts (2+5 vs 0+4); pair 2 was heavy for both. Not an outcome, not explained.
* **Supported:** the fixed H1 did not clearly hurt in two paired runs, and the targeted failure appeared once in the H0 arm and never in the H1 arm.
  **Not supported:** that H1 improves AUC, that the notes / decision policy / memory each contribute, or that the automatic improvement method works
  (H1 was written from old evidence by a single improver session; it also switches on memory rendering, so the advice appears twice in the notes).
* Reaching a real answer on the failure rate needs many more runs: about $1.5 per run, so roughly 8-10 pairs (~$25) to see a rate difference
  like 1/3 vs 0.

## Bookkeeping
`run_record.json` for pair 2 says `replicate: 1` (collect was run without `--replicate`); the correct pairing is by job id and in
`runs_index.jsonl`/`RESULTS.json`. Run directories are in `runs/random-acts-of-pizza/` (verified file-by-file against CRC); CRC has no running jobs.
