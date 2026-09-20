#!/usr/bin/env python3
"""Fix the agent-mode decision prompt in an INSTALLED aide package (run inside the container, agent conda env).

Bug: without a harness bundle, `_ordered_selection_prompt` puts `[(title, text), ...]` lists into the prompt dict; the
prompt renderer calls `.strip()` on each list element and fails with "'tuple' object has no attribute 'strip'", so
every decision call fails BEFORE it is sent and AIDE silently falls back to the rule-based policy.  Agent mode then
runs as rule mode.

Fix: flatten channel sections into `title -> text`, the shape the v2 branch of the same function already produces.
Idempotent; refuses to touch a file unless the buggy block occurs exactly once.

    python apply_agent_decision_fix.py [--check] [PACKAGE_DIR]     (default: the installed `aide` package)
"""
from __future__ import annotations

import hashlib
import os
import sys

OLD = '''    if not _selection_harness_v2():
        out = dict(base)
        out.update(channel_sections)
        return out
'''
NEW = '''    if not _selection_harness_v2():
        # channel_sections maps channel -> [(title, text), ...]; the prompt renderer wants title -> text
        out = dict(base)
        for entries in channel_sections.values():
            for title, value in entries:
                out.setdefault(title, value)
        return out
'''
MARKER = "def _ordered_selection_prompt"


def patch_text(text: str) -> tuple:
    """(new_text, status) with status in {"patched", "already_patched", "not_applicable"}."""
    if NEW in text:
        return text, "already_patched"
    if text.count(OLD) != 1:
        return text, "not_applicable"
    return text.replace(OLD, NEW), "patched"


def find_targets(package_dir: str) -> list:
    hits = []
    for root, _dirs, files in os.walk(package_dir):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8", errors="ignore") as f:
                    if MARKER in f.read():
                        hits.append(path)
    return sorted(hits)


def main(argv: list) -> int:
    check_only = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    if args:
        package_dir = args[0]
    else:
        import aide
        package_dir = os.path.dirname(aide.__file__)
    targets = find_targets(package_dir)
    if len(targets) != 1:
        print(f"ERROR: expected exactly one file defining {MARKER!r} under {package_dir}, found {targets}")
        return 2
    path = targets[0]
    text = open(path, encoding="utf-8").read()
    new, status = patch_text(text)
    if status == "not_applicable":
        print(f"ERROR: {path}: the buggy block does not occur exactly once; refusing to edit")
        return 3
    if status == "patched" and not check_only:
        open(path, "w", encoding="utf-8").write(new)
    print(f"{status}: {path} md5={hashlib.md5((new if not check_only else text).encode()).hexdigest()}")
    return 0 if (status == "already_patched" or not check_only) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
