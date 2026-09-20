# Patches for the research repo (NOT applied)

`metric_guard_skip_fold_counts.patch` fixes the metric guard that turned `Mean AUC score (5-fold CV): 0.62`
into a recorded validation value of **5.0** (the fold count) in the 2026-09-19 pilot.

* Cause: `_number_after_metric` returns the first number after the last metric name; in `... (5-fold CV): 0.62`
  that number is the `5` of "5-fold". The reviewer LLM's correct value was then overridden by the guard.
* Fix: skip fold/split counts (`5-fold`, `5 folds`, `cv=5`, `n_splits=5`). The patch also adds a regression
  test to the research repo's own `tests/test_metric_runtime_patches.py`.
* Verified in a scratch copy: the research repo's 7 existing guard tests + the new one pass; the new one FAILS
  on the unpatched module; `git apply --check` succeeds on the current research repo. Nothing was applied.

## Applying it - it must reach the OVERLAY or it changes nothing

The guard that runs inside a job is **not** `scripts/_metric_parser_runtime.py`. `08_patch_feedback_interface.sh`
copies that file into the installed package as `aide/_metric_parser.py`, inside
`apptainer/agent_fixes.overlay`. On 2026-09-20 both copies were byte-identical (md5 315eab0f...), so the bug
is live in every job until the overlay is re-patched. The overlay is shared by every experiment.

```bash
# local research repo
cd MLE-bench_AIDE && git apply real-rsi-mvp/patches/metric_guard_skip_fold_counts.patch
python -m pytest tests/test_metric_runtime_patches.py -q

# CRC (~/mlebench-aide is not a git checkout)
cd ~/mlebench-aide && patch -p1 < ~/real-rsi-mvp/patches/metric_guard_skip_fold_counts.patch
cp -p apptainer/agent_fixes.overlay apptainer/agent_fixes.overlay.bak-before-guard-fix   # 1 GB backup
bash scripts/08_patch_feedback_interface.sh                                                # rewrites the overlay copy
# verify the overlay now holds the patched parser (read-only mount):
apptainer exec --overlay apptainer/agent_fixes.overlay:ro apptainer/mlebench-aide.sif bash -lc \
  'source /opt/conda/etc/profile.d/conda.sh && conda activate agent && \
   md5sum $(python -c "import aide,os;print(os.path.dirname(aide.__file__))")/_metric_parser.py'
md5sum scripts/_metric_parser_runtime.py        # the two md5 values must match
```

Runs that already finished keep their recorded (wrong) values; only new runs are affected.
