#!/usr/bin/env python3
"""Standard task entrypoint: run this task's AIDE mode through the RSI wrapper.

    python tasks/random_acts_of_pizza/run.py --mode rule --harness H0 --round 0
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsi_mvp.cli import main  # noqa: E402

if __name__ == "__main__":
    main(["submit", "--task", "random_acts_of_pizza", *sys.argv[1:]])
