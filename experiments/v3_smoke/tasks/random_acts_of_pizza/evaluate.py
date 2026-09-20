#!/usr/bin/env python3
"""Fixed final evaluator: read the official MLE-bench grade of a finished run.

    python tasks/random_acts_of_pizza/evaluate.py <run_dir>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsi_mvp.trajectory import read_grade  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(read_grade(Path(sys.argv[1])), indent=2))
