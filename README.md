# Trustworthy ML Agent

A minimal harness-improvement loop around existing AIDE agents on MLE-bench tasks.

```text
task + harness H_t → AIDE run → trajectory + verifier rewards
                  → memory → LLM edits harness → checks → H_{t+1}
```

The loop is implemented. Improvement in task performance has not yet been established.

## Structure

- `rsi_mvp/`: task loading, execution adapter, verifiers, memory, improver and versioning.
- `tasks/`: training and held-out task configurations.
- `tests/`: synthetic, offline tests; no API key or cluster required.
- `vendor/`: the issue scanner used by the verifier bank, with provenance.
- `patches/` and `tools/`: AIDE compatibility fixes, overlay checks and cost monitoring.

Experiment records, generated harnesses, logs and historical reports are local artifacts,
not part of the source distribution.

## Install and test

From this checkout, with Python 3.9 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,verifiers]'
python -m pytest -q
```

## Run one improvement cycle

Live execution requires an existing MLE-bench/AIDE checkout with its SGE submission
scripts, prepared task data, Apptainer image and compatible overlay. Task packages
reference that environment; this repository does not install it.

Set `MLEBENCH_AIDE_ROOT` to the execution checkout, `RSI_OVERLAY_PATH` to a prepared
overlay, and `OPENAI_API_KEY` for the improver. Compute-node credentials are configured
by the execution checkout. See [patches/README.md](patches/README.md) for compatibility requirements.

Keep runtime state in its own local Git repository so generated versions are recorded
without entering the source repository:

```bash
mkdir -p .local/state
git init .local/state

python -m rsi_mvp --state-root .local/state init --task random_acts_of_pizza --mode agent
python -m rsi_mvp --state-root .local/state submit --task random_acts_of_pizza --mode agent --round 0 --dry-run
```

Review the task budget before submitting a paid run. Remove `--dry-run` to submit.
After the job finishes, replace `JOB_ID` below with its returned ID:

```bash
python -m rsi_mvp --state-root .local/state collect --task random_acts_of_pizza --mode agent --round 0 --job-id JOB_ID
python -m rsi_mvp --state-root .local/state improve --task random_acts_of_pizza --mode agent --round 0 --max-cost 0.40
```

The next submission uses the latest harness. Repeat with the next round number.
`--max-cost` checks accumulated improver spending before another call; the last call
can exceed that threshold. Runner costs are separate (`tools/cost_watchdog.py`).
Use `python -m rsi_mvp --help` for the resumable driver and held-out evaluation commands.

## Method and boundaries

Each task supplies `task.yaml`, `aide_config.yaml` and `verifier_config.yaml`.
Six verifiers report a reward vector and supporting evidence. Memory and version
history accompany that evidence in the improver's read-only context.

The default improver directly edits a private harness directory:

- `prompt_notes.md` and `decision_policy.md`: instructions and decision guidance.
- `rule_config.json`: bounded settings for rule mode.
- `memory_policy.json`: memory selection and rendering settings.
- `observation_policy.json`: optional prediction-profile observations.
- `hooks/select_memory.py` and `hooks/render_notes.py`: optional memory/context logic.
- `CHANGES.md`: explanation of edits.

It may rewrite or remove content and change multiple components. Scope and rendering
checks run before a new version is saved. Rendered notes are frozen with that version.
Passing these checks means the candidate is runnable, not that its performance improved.
The legacy JSON-patch interface remains available through `--improver patch`.

AIDE code, task data and evaluators are outside the editable boundary. Hook restrictions
provide defense in depth, not isolation against malicious code. Prediction profiles reach
the feedback reviewer; the final selector sees them only if retained in reviewer findings.

Only training tasks may expose official scores to the improver. Freeze records the selected
harness and memory before evaluation on a held-out task; an exposure ledger guards this split.
The driver halts on `no_change`, rejected edits or errors. Recollecting with `--force` can
duplicate memory records. Neither repeated rounds nor fixed first drafts guarantee improvement
or deterministic LLM runs.
