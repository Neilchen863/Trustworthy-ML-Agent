# Corrections to experiments/v3_pairs (2026-09-21)

* `run_record.json` of jobs 1460580 (H0) and 1460581 (H1) said `replicate: 1`; they were submitted as replicate 2
  (`rounds/runs_index.jsonl` was right). Cause: `rsi collect` had no `--replicate` option and defaulted to 1. The records now say 2 and
  carry a `metadata_correction` block with the original value. `collect_round` now looks the replicate up from the submission record
  (test `test_collect_takes_the_replicate_from_the_submission_record`). Nothing else in the records changed; scores, verifier results and
  costs were re-derived from the raw run directories and match (see `docs/v3_pairs_results_20260921.md`, "Verification").
* The delivered notes of the two H1 runs differ in one character sequence only: the port of the local grading server
  (`localhost:40473` vs `localhost:40861`) inside the task text. The harness text is identical in both runs.
