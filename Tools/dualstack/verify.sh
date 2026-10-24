#!/bin/sh
# Run the dual-stack verifiers.
#
#   Tools/dualstack/verify.sh quick      # checker on core tests, recursion table, golden traces, instr counts
#   Tools/dualstack/verify.sh full       # quick + whole test suite under the checker + oracle quick + refleaks
#
# Environment:
#   BASELINE_PYTHON   interpreter used to (re)record goldens when they are missing
#   PYTHON_CONTROLSTACK_CHECK  window (default) | full | off
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)
GOLDEN="$HERE/golden"
MODE=${1:-quick}
if [ -x "$SRC/python.exe" ]; then PY="$SRC/python.exe"; else PY="$SRC/python"; fi
export PYTHON_CONTROLSTACK_CHECK=${PYTHON_CONTROLSTACK_CHECK:-window}
status=0
step() { echo; echo "==== $*"; }

step "1. invariant checker: smoke"
"$PY" -c 'import _testinternalcapi as t; print(t.check_control_stack(strict=True))' || status=1

step "1. invariant checker: core tests"
CORE="test_generators test_coroutines test_asyncgen test_exceptions test_frame test_sys_settrace test_monitoring test_traceback test_asyncio.test_tasks"
if [ "$MODE" = "full" ]; then
    "$PY" -m test -j0 || status=1
else
    "$PY" -m test -j0 $CORE || status=1
fi

step "2. recursion table"
if [ -f "$GOLDEN/recursion_baseline.json" ]; then
    "$PY" "$HERE/recursion_table.py" --compare "$GOLDEN/recursion_baseline.json" || status=1
else
    echo "no $GOLDEN/recursion_baseline.json; record it with: BASELINE_PYTHON recursion_table.py --json ..."
    "$PY" "$HERE/recursion_table.py" || status=1
fi

step "3. golden frame traces"
if [ -f "$GOLDEN/calls.json" ]; then
    "$PY" "$HERE/frame_trace.py" compare "$GOLDEN" || status=1
else
    echo "no goldens in $GOLDEN; record them with: BASELINE_PYTHON frame_trace.py record --out $GOLDEN"
fi

step "4. instruction counts"
if command -v perf >/dev/null 2>&1 || command -v valgrind >/dev/null 2>&1; then
    if [ -f "$GOLDEN/instr_baseline.json" ]; then
        "$PY" "$HERE/instr_count.py" --compare "$GOLDEN/instr_baseline.json" || status=1
    else
        "$PY" "$HERE/instr_count.py" || status=1
    fi
else
    echo "no perf/valgrind on this host; skipped (run on Linux)"
fi

if [ "$MODE" = "full" ]; then
    step "5. oracle quick (needs root on macOS)"
    "$PY" "$HERE/oracle_quick.py" || status=1
    step "6. refleaks"
    sh "$HERE/refleak.sh" || status=1
fi

echo
echo "verify.sh $MODE: exit $status"
exit $status
