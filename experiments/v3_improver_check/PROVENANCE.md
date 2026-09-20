# v2 provenance notes
- Round-0 rows in rounds/runs_index.jsonl (jobs 1459602-1459607) omit OVERLAY_PATH and AIDE_SEED_CODE from their
  recorded env: a recording bug fixed on 2026-09-20 (rows submitted after the fix carry them). The runs themselves
  used both: runs/sge-<job>.out shows "Using agent-fix overlay: /users/jchen53/real-rsi-mvp/overlays/v2/agent_fixes_v2.overlay" and each
  run_config.txt shows seed_code=.../rsi_cd41d76ba2.code.py.
- Dedicated overlay = a copy of the shared overlay with ONLY aide/_metric_parser.py replaced by the patched parser
  (44 files fingerprinted, exactly 1 differs). The shared overlay and shared scripts were not modified.
- Smoke probes: job 1459599 (dedicated overlay) recorded 0.6323, job 1459600 (shared overlay, control) recorded 5.0.

## 2026-09-20 - v2 was STOPPED by the project owner; treat its data as invalid/incomplete
- All v2 jobs were deleted (rule 1459603/1459604 and agent 1459605/1459607 while running); no driver is running.
- Only two runs finished: rule 1459602 and agent 1459606. The agent-mode arm never made an LLM decision (every step logged
  "[agent search] failed (\x27tuple\x27 object has no attribute \x27strip\x27) -> falling back to rule-based policy"), so
  agent-mode results from the pilot and from v2 are rule-policy runs. Cause and reproduction: see the session notes; not fixed.
