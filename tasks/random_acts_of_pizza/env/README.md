Fixed environment (never modified by RSI).  It lives on the ND CRC cluster and is
named here, not copied:

- image     : apptainer/mlebench-aide.sif  (AIDE thesofakillers/aideml@v6.3.3 + patches)
- data      : prepared with `scripts/02_prepare_data.sh random-acts-of-pizza`
- grader    : MLE-bench `mlebench grade`, run by scripts/run_aide.sh
- resources : 8 CPUs, no GPU

The RSI wrapper reaches it only through `$MLEBENCH_AIDE_ROOT/sge/submit.sh`.
