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

## How to read it (revised 2026-09-21; two earlier statements are withdrawn, see the end)
Two pairs are 2 + 2 runs from a heavy-tailed process. What the numbers do and do not carry:
* **AUC.** The two pair differences are +0.103 and -0.024 (sd of the two = 0.09). The 95% t-interval of their mean (df = 1) is roughly
  +/-0.8, i.e. it covers everything: **the AUC data are uninformative about H1**, in either direction. The spread inside one arm
  (H0: 0.500 vs 0.668) is larger than the H1 - H0 gap. The +0.103 is entirely the one H0 run that submitted a leaked node.
* **Target failure (per run, exact 95% CI).** H0 1/2 [0.01, 0.99]; H1 0/2 [0.00, 0.84]; stock H0 under this fixed setup 1/3 [0.01, 0.91].
  Node counts (16/50 vs 0/50) are not independent (a leaky lineage contaminates many nodes), so the unit is the run. This is one event.
* **Was H1 "not worse"?** In these four runs there is no sign of a large degradation, and the pre-registered stop rule (AUC diff <= -0.04 with no mirage
  reduction) did not fire. Two pairs cannot exclude a substantial degradation or benefit; "not clearly worse" is the most that can be said, and only as a description of these runs.
* **Supported:** pipeline and cost control work end to end with a working decision LLM (4 valid runs, $5.99, no watchdog kill); the
  pre-registered procedure was followed; agent mode with real decisions can pick a leaked perfect-validation node and submit a constant (seen once).
  **Not supported:** that H1 changes AUC or the failure rate, any split into notes / policy / memory, any statement about the automatic improvement method.
* Diagnostics (not outcomes): H1 runs used more execution time in pair 1 (46 vs 9 min) and had more timeouts (2 + 5 vs 0 + 4); unexplained.
* Policy note: the improver may switch memory rendering on or off by itself (decided by the user, 2026-09-21); H1 does, so its advice
  appears twice in the notes. That is part of what H1 is, not a reason to regenerate it.

## Verification (free, done 2026-09-21)
Recomputed from the raw run directories (not from the collected records): official score and validity from `grade_report.txt`, cost from `token_usage.jsonl`,
node/decision counts from `journal.json` / `aide.log`, submission contents, final node, overlay from the job logs, run configuration.
All match the table. All four mounted overlay v3; all four started from the same pinned first draft (node 0 code hash cd41d76ba2); `run_config.txt` differs
only in host, run id and prompt variant; both H1 runs received the persisted `rendered_notes.txt` verbatim (their notes differ only by the port of the local
grading server); the H0 runs received no harness text. The failing H0 run's final node had validation 1.0 and a submission of one constant value (1162 rows).

## Withdrawn statements (first version of this report)
1. "Run-to-run sd about 0.028, so differences below ~0.055 are unreadable." The 0.028 came from 7 runs of other configurations (rule / fallback-rule, free
   and pinned drafts, 500 nodes), its own 95% interval is about [0.018, 0.062], and these four runs (sd 0.074) do not resemble it. No noise level for the
   25-node agent configuration is known, so no detectable-difference figure can be stated.
2. "About 8-10 pairs, ~$25, would show a failure-rate difference like 1/3 vs 0." No basis: the base rate is estimated from 3 runs (CI [0.01, 0.91]).
   Illustration only, assuming stock 1/3 vs H1 0 and one-sided Fisher 0.05: 10 runs per arm gives power 0.43, 15 gives 0.78 (about $45 for 30 runs);
   with a lower base rate or a smaller effect it is far worse (stock 0.15 vs 0: power 0.18 at 15 per arm). The right first step of any extension is to
   estimate the stock failure rate and the AUC noise of this configuration, not to power a comparison of H1.

## Bookkeeping
Pair-2 `run_record.json` files said `replicate: 1`; corrected to 2 with the original value kept (`experiments/v3_pairs/CORRECTIONS.md`), and `rsi collect` now
takes the replicate from the submission record. Run directories are in `runs/random-acts-of-pizza/` (verified file-by-file against CRC); CRC has no running jobs.
