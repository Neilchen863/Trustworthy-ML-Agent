#!/bin/bash
# Build a DEDICATED overlay for real-RSI agent-mode runs (run on CRC).  Leaves the shared overlay untouched.
#
#   v3 overlay = v2 overlay (shared overlay + metric-guard fix)  +  decision-prompt fix
#
# Usage:  bash tools/build_agent_fix_overlay.sh [AIDE_ROOT]      (default ~/mlebench-aide)
set -euo pipefail
AIDE_ROOT="${1:-$HOME/mlebench-aide}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/overlays/v2/agent_fixes_v2.overlay"
DST="$HERE/overlays/v3/agent_fixes_v3.overlay"
source "$AIDE_ROOT/scripts/_common.sh"
load_apptainer
[[ -f "$SRC" ]] || { echo "missing $SRC" >&2; exit 1; }
[[ -f "${SIF_PATH}" ]] || { echo "missing image ${SIF_PATH}" >&2; exit 1; }
mkdir -p "$(dirname "$DST")"
[[ -e "$DST" ]] && { echo "$DST already exists; remove it to rebuild" >&2; exit 1; }
cp -p "$SRC" "$DST"

list_files() {   # md5 of every file of the aide package, read-only mount
  "${APPTAINER_BIN}" exec --overlay "$1:ro" "${SIF_PATH}" bash -lc 'set -e
    source /opt/conda/etc/profile.d/conda.sh && conda activate agent
    d=$(python -c "import aide,os;print(os.path.dirname(aide.__file__))"); cd "$d" && find . -name "*.py" | sort | xargs md5sum'
}

echo "== applying the decision-prompt fix inside $DST"
"${APPTAINER_BIN}" exec --overlay "$DST" --bind "$HERE/patches:/mnt/patches:ro" "${SIF_PATH}" bash -lc 'set -e
  source /opt/conda/etc/profile.d/conda.sh && conda activate agent
  python /mnt/patches/apply_agent_decision_fix.py'

echo "== files that differ from the v2 overlay (expect exactly one)"
diff <(list_files "$SRC") <(list_files "$DST") || true
echo "== shared overlay untouched:"; ls -l "$AIDE_ROOT/apptainer/agent_fixes.overlay"
