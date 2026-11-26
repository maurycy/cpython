#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
REV=c0ee4bb03e3
exec >>"$B/pipeline4.log" 2>&1
stage() { echo "=== $(date -u +%FT%TZ) $*"; }
stage stopping full pyperformance
tmux kill-session -t bench2 2>/dev/null
pkill -f pyperformance 2>/dev/null
sleep 5
pkill -f "bench/venv" 2>/dev/null
rm -f "$R"/pp-full-*

build_align() {
    n=$1; attr=$2
    cd "$B/src" && git worktree add --detach "$B/$n" $REV || return 1
    cd "$B/$n" || return 1
    sed -i "s/^Py_NO_INLINE PyObject\* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE\$/Py_NO_INLINE PyObject* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE $attr/" Python/ceval.c
    git diff --stat | grep -q ceval.c || return 1
    git diff >"$R/$n.diff"
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    make -s -j12 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}
A32='__attribute__((aligned(32)))'
O16='__attribute__((aligned(32), patchable_function_entry(16, 16)))'
if ! [ -e "$ST/align-built.ok" ]; then
    stage building a32a o16a
    build_align a32a "$A32" & p1=$!
    build_align o16a "$O16" & p2=$!
    wait $p1; r1=$?; wait $p2; r2=$?
    stage building a32b o16b "(a32a=$r1 o16a=$r2)"
    build_align a32b "$A32" & p1=$!
    build_align o16b "$O16" & p2=$!
    wait $p1; r1=$?; wait $p2; r2=$?
    stage built "(a32b=$r1 o16b=$r2)"
    touch "$ST/align-built.ok"
fi
ALL="base base2 base3 a32a a32b o16a o16b v3 v3b v3c"
for n in $ALL; do
    nm -S "$B/$n/python" | awk -v n=$n '/ _PyEval_EvalFrameDefault$/{print n, $1, $2}'
done >"$R/align-addr.txt"
stage align micro
GEN4=call_return,recursion_depth200,property_get,gen_for_loop,gen_send,next_from_c
REVERSED=$(echo $ALL | tr ' ' '\n' | tac | tr '\n' ' ')
for n in $ALL $REVERSED; do
    DUALSTACK_ONLY=$GEN4 "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
        --affinity 4 --inherit-environ DUALSTACK_ONLY \
        --append "$R/micro4-$n.json" >>"$R/micro4-$n.txt" 2>&1
done
touch "$ST/align.ok"
stage resuming main pipeline
exec "$T/pipeline.sh"
