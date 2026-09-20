# v3_pairs: does the fixed H1 deserve further study? (pre-registered 2026-09-21, before any run)

Object: **H1 as produced by the improver session in `experiments/v3_improver_check`** (harness sha256 `8a529e34...`,
rendered notes sha256 `1db7637e...`, `memory_policy.render = lessons`), against the stock **H0**. Nothing is improved or edited
during this experiment. The whole H1 is tested as one object; notes / decision policy / memory are NOT separated.
H1 came from old failure evidence (v2 agent round 0), not from a run of the current setup.

## Fixed configuration (identical for all four runs; only PROMPT_VARIANT differs)
agent mode with the decision-prompt fix (overlay v3, `_agent_decision.py` md5 c05197b4...), gpt-4o-2024-08-06 for code, feedback and decisions,
pinned neutral first draft (seed sha cd41d76ba2), **25 nodes**, 2 h wall-clock ceiling (the node count is the intended limit), 10 min per execution,
8 CPUs, tree top-k 5 / recent 5. Pair k = H0 replicate k and H1 replicate k, submitted together. LLM sampling is not pinned; the data split inside AIDE is not
controlled by us.

## Budget (hard)
Each run is killed by `tools/cost_watchdog.py` at **$1.75** (list price, polled every 45 s; overshoot at most ~one poll of spend). 4 runs <= $7.00.
Already spent in this project session: about $1.35. Total stays under $10.

## Outcomes, fixed in advance
1. **Primary**: official AUC difference H1 - H0 for each pair. All four scores are reported; no run is dropped or preferred.
2. **Auxiliary (the problem H1 targets)**: `validation_mirage` node rate as the interval [lower, upper] with n_flagged / n_units for every run.
3. **Diagnostics (not outcomes)**: decision calls that succeeded vs fell back, node count, buggy nodes, timeouts, NN-abandoned flag, execution minutes,
   cost. A normal debug failure is not a policy failure.

## Validity and stopping rules
* A run is **valid** if it was graded with a valid submission, its delivery check passed, it made 0 decision fallbacks, it was not killed by the watchdog,
  and its environment equals its partner's except PROMPT_VARIANT. A pair is valid if both runs are valid. A run with < 20 nodes is flagged as truncated.
* Pair 1 invalid -> **stop and investigate; no pair 2.**
* Pair 1 valid and H1 clearly worse (AUC difference <= -0.04 **and** H1's mirage lower bound >= H0's lower bound) -> **stop; no pair 2.**
* Otherwise run pair 2 (it is most useful when pair 1 is ambiguous).

## What the result can and cannot say
Measured run-to-run sd of AUC on this task is about 0.028 (7 long runs), so the mean of two pair differences has sd about 0.028: a difference is
only readable if it is larger than about 0.055. If H1 has no effect, "H1 wins both pairs" happens 25% of the time. At ~25 nodes per run the mirage
denominator is small (baseline about 0-3 hits). So this experiment can flag a clearly worse H1 or a large effect; it cannot show that H1 helps, and it cannot show that
the automatic improvement method works. A win would only mean "this fixed H1 is worth a larger test".
