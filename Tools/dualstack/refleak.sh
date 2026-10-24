#!/bin/sh
# Refleak runs for the frame-heavy tests, optionally under a Py_STACKREF_DEBUG
# build living in a detached worktree next to this one.
#
#   Tools/dualstack/refleak.sh              # -R 3:3 with ./python(.exe) of this tree
#   Tools/dualstack/refleak.sh --stackref   # also build+run a Py_STACKREF_DEBUG tree
#   TESTS="test_generators" Tools/dualstack/refleak.sh
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)
TESTS=${TESTS:-"test_generators test_coroutines test_asyncgen test_sys_settrace test_sys_setprofile test_monitoring test_exceptions test_frame test_traceback test_inspect test_external_inspection test_sampling_profiler test_asyncio.test_tasks test_asyncio.test_futures test_threading"}
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}

python_in() {
    if [ -x "$1/python.exe" ]; then echo "$1/python.exe"; else echo "$1/python"; fi
}

run_refleaks() {
    py=$1
    echo "== refleaks with $py"
    PYTHON_CONTROLSTACK_CHECK=${PYTHON_CONTROLSTACK_CHECK:-window} \
        "$py" -m test -R 3:3 -j "$JOBS" $TESTS
}

run_refleaks "$(python_in "$SRC")"

if [ "${1:-}" = "--stackref" ]; then
    DIR=${STACKREF_DIR:-"$SRC-stackref"}
    HEAD=$(git -C "$SRC" rev-parse HEAD)
    if [ ! -d "$DIR" ]; then
        git -C "$SRC" worktree add --detach "$DIR" "$HEAD"
    else
        git -C "$DIR" checkout --detach "$HEAD"
    fi
    if [ ! -f "$DIR/Makefile" ]; then
        (cd "$DIR" && ./configure --with-pydebug CFLAGS="-DPy_STACKREF_DEBUG" -q)
    fi
    (cd "$DIR" && make -j "$JOBS" -s)
    run_refleaks "$(python_in "$DIR")"
fi
