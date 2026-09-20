# random-acts-of-pizza (training task)

Predict whether a Reddit "Random Acts of Pizza" request receives a pizza
(AUC-ROC, higher is better; ~5.6k JSON rows, CPU only).  The task description
the agent actually sees is MLE-bench's own; this file is only for humans.

The mutable object is the harness (prompt notes, mode-specific decision policy,
memory) - never this task, its data or its evaluator.
